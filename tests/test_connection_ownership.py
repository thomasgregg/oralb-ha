"""Regression tests for connection, source and session ownership."""

from __future__ import annotations

import asyncio
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from tests.test_battery_refresh import const, sensor
from tests.test_disconnect_cleanup import BackendClient
from tests.test_charger_discovery_retry import Client, OTHER_BRUSH, OWN, radio
from tests.test_post_session_beta import brush
from tests.test_session_reconciliation import FIRST_ID, _native_time, _record

from oralb_live import charger
from oralb_live.charger_protocol import ChargerCommand


async def direct_callback(h, client, uuid):
    subscriptions = {}

    async def subscribe(characteristic, callback):
        subscriptions[characteristic] = callback

    client.start_notify = subscribe
    h.c._client = client
    await h.c._async_subscribe(client)
    return subscriptions[uuid]


async def native_reply(bridge, client, command, payload):
    async def write(_uuid, frame, *, response):
        if frame == charger.IOSENSE_PROTOCOL_END:
            bridge._on_read(None, bytearray.fromhex(payload))

    client.write_gatt_char = write
    return await bridge._async_get(command)


@pytest.mark.parametrize("slot", ["replacement", "stopped"])
@pytest.mark.parametrize(
    "uuid,key,old_sample,current_sample",
    [
        (const.CHAR_STATUS_BLOB, "battery", b"\x17", b"\x3f"),
        (const.CHAR_BRUSH_TIME, "time", b"\x00\x05", b"\x00\x25"),
    ],
)
def test_old_direct_notification_cannot_change_brush_state(slot, uuid, key, old_sample, current_sample):
    async def exercise():
        async with brush() as h:
            old = BackendClient(connected=True)
            old_notify = await direct_callback(h, old, uuid)
            await h.c._async_disconnect()
            fresh = BackendClient(connected=True)
            new_notify = await direct_callback(h, fresh, uuid)
            char = SimpleNamespace(uuid=uuid)
            new_notify(char, bytearray(current_sample))
            expected = h.c.data[key]
            if slot == "stopped":
                await h.c.async_stop()
            old_notify(char, bytearray(old_sample))
            assert h.c.data[key] == expected

    asyncio.run(exercise())


@pytest.mark.parametrize("link_replaced", [False, True])
def test_late_initial_battery_read_cannot_overwrite_newer_notification(link_replaced):
    async def exercise():
        async with brush() as h:
            seed = BackendClient(connected=True)
            notify = await direct_callback(h, seed, const.CHAR_STATUS_BLOB)
            entered, release = asyncio.Event(), asyncio.Event()

            async def read(client, uuid, label):
                if uuid == const.CHAR_STATUS_BLOB:
                    return bytearray((23,))
                if uuid == const.CHAR_MODEL_ID:
                    entered.set()
                    await release.wait()
                return None

            with patch.object(h.c, "_async_sync_read", read):
                task = asyncio.create_task(h.c._async_initial_reads(seed))
                await entered.wait()
                if link_replaced:
                    await h.c._async_disconnect()
                    fresh = BackendClient(connected=True)
                    notify = await direct_callback(h, fresh, const.CHAR_STATUS_BLOB)
                notify(SimpleNamespace(uuid=const.CHAR_STATUS_BLOB), bytearray((63,)))
                assert h.c.data["battery"] == 63
                release.set()
                await task
                assert h.c.data["battery"] == 63

    asyncio.run(exercise())


def test_direct_release_timer_cannot_close_a_replacement_client():
    async def exercise():
        async with brush() as h:
            h.c.mode = const.CONNECTION_MODE_LIVE
            old = BackendClient(connected=True)
            notify = await direct_callback(h, old, const.CHAR_STATE)
            notify(SimpleNamespace(uuid=const.CHAR_STATE), bytearray((115,)))
            await h._advance(1)
            old.is_connected = False
            h.c._handle_client_disconnect(old)
            await h._ready()
            fresh = BackendClient(connected=True)
            await direct_callback(h, fresh, const.CHAR_STATE)
            await h._advance(const.RELEASE_GRACE_SECONDS)
            assert fresh.is_connected
            assert h.c._client is fresh

    asyncio.run(exercise())


@pytest.mark.parametrize("kind", ["get", "passthrough"])
def test_queued_charger_request_leaves_old_client_alone_after_reconnect(kind):
    async def exercise():
        async with radio() as r:
            bridge = r.bridge
            old, fresh = BackendClient(connected=True), BackendClient(connected=True)
            old.write_gatt_char = AsyncMock(side_effect=charger.BleakError("old link"))
            fresh.write_gatt_char = AsyncMock()
            bridge._client = old
            await bridge._request_lock.acquire()
            task = asyncio.create_task(
                bridge._async_get(ChargerCommand.BRUSH_DATA)
                if kind == "get"
                else bridge._async_passthrough("FF05")
            )
            try:
                await r.advance()
                old.is_connected = False
                bridge._handle_client_disconnect(old)
                bridge._client = fresh
            finally:
                bridge._request_lock.release()
            await r.advance(2)
            assert task.done()
            assert task.result() is None
            old.write_gatt_char.assert_not_awaited()
            fresh.write_gatt_char.assert_not_awaited()

    asyncio.run(exercise())


def test_disconnected_charger_tick_cannot_republish_live_true():
    async def exercise():
        async with radio() as r:
            bridge = r.bridge
            old = Client(r, OWN, bridge._on_disconnect, None)
            bridge._client = old
            bridge._session_running = True
            bridge._session_confirmed = True
            bridge.address = OWN
            entered, release = asyncio.Event(), asyncio.Event()

            async def delayed_passthrough(short_uuid):
                if short_uuid == "FF0B":
                    entered.set()
                    await release.wait()
                return None

            with patch.object(bridge, "_async_passthrough", delayed_passthrough):
                task = asyncio.create_task(bridge._async_live_loop())
                try:
                    await entered.wait()
                    old.is_connected = False
                    bridge._handle_client_disconnect(old)
                    assert bridge.parent.data["live"] is False
                    release.set()
                    await r.advance()
                    assert bridge.parent.data["live"] is False
                finally:
                    task.cancel()
                    await asyncio.gather(task, return_exceptions=True)

    asyncio.run(exercise())


def test_matched_active_bridge_recovers_after_remote_loss_without_new_payload():
    async def exercise():
        async with radio() as r:
            bridge = r.bridge
            bridge.async_start()
            r.advert(OWN, bits=0x48)
            await r.advance(1)
            assert bridge.address == OWN
            active_client = Client(r, OWN, bridge._on_disconnect, None)
            bridge._client = active_client
            bridge._session_running = True
            bridge._session_confirmed = True
            r.advert(OWN, bits=0x48)
            active_client.is_connected = False
            active_client.disconnected(active_client)
            await r.advance(30)
            assert bridge.address == OWN
            assert active_client.released
            assert len(r.attempts_for(OWN)) >= 2

    asyncio.run(exercise())


def test_charger_history_parts_can_cross_attempt_boundary_today():
    async def exercise():
        async with radio() as r:
            bridge = r.bridge
            bridge.address = OWN
            bridge._client = Client(r, OWN, bridge._on_disconnect, None)
            record, rtc = bytes.fromhex("0102030405060708"), bytes.fromhex("11223344")

            async def first(short_uuid):
                return record if short_uuid == "FF29" else None

            async def second(short_uuid):
                return rtc if short_uuid == "FF22" else None

            with patch.object(
                bridge.parent, "_async_apply_session_record", AsyncMock(return_value="settling")
            ) as reconcile:
                with patch.object(bridge, "_async_passthrough", first):
                    await bridge._async_post_session_sync(force=True)
                reconcile.assert_not_awaited()
                await r.advance(61)
                with patch.object(bridge, "_async_passthrough", second):
                    await bridge._async_post_session_sync(force=True)
                reconcile.assert_awaited_once()
                assert reconcile.await_args.args == (record, rtc)

    asyncio.run(exercise())


@pytest.mark.parametrize("verified", [False, True])
def test_native_charger_run_status_requires_verified_association(verified):
    async def exercise():
        async with radio() as r:
            bridge = r.bridge
            if verified:
                bridge.address = OWN
            client = Client(r, OWN, bridge._on_disconnect, None)
            bridge._client = client
            with patch.object(bridge, "_ensure_live_task"):
                reply = await native_reply(bridge, client, ChargerCommand.BRUSH_STATUS, "33c004")
                assert reply.value == "run"
                assert bridge.session_running is verified
                assert bridge.parent.data["live"] is verified

    asyncio.run(exercise())


def test_charger_get_reply_must_match_operation_as_well_as_command():
    async def exercise():
        async with radio() as r:
            bridge = r.bridge
            client = BackendClient(connected=True)

            async def write(uuid, payload, *, response):
                if payload == charger.IOSENSE_PROTOCOL_END:
                    bridge._on_read(None, bytearray.fromhex("39c100112233445566"))
                    bridge._on_read(None, bytearray.fromhex("39c000aabbccddeeff"))

            client.write_gatt_char = write
            bridge._client = client
            result = await bridge._async_get(ChargerCommand.BRUSH_DATA)
            assert result.operation == 0xC0
            assert result.value["brush_mac"] == "AA:BB:CC:DD:EE:FF"

    asyncio.run(exercise())


def test_deferred_charger_record_cannot_acknowledge_generation_created_after_scheduling():
    async def exercise():
        async with brush() as h:
            now = h.clock.utcnow()
            h.c.data["protocol_version"] = 7
            h.c._session_generation = 1
            h.c._session_pending_sync = True
            h.c._charger_session_record = _record(now - timedelta(seconds=150), 60)
            h.c._charger_session_rtc = _native_time(now).to_bytes(4, "little")
            h.c._charger_session_record_sampled_at = now
            h.c._charger_session_rtc_sampled_at = now
            h.c._schedule_deferred_charger_session_record()
            h.c._session_generation = 2
            await h._ready()
            assert h.c._processed_session_generation < 2
            assert h.c._session_pending_sync

    asyncio.run(exercise())


def test_charger_idle_release_does_not_close_replacement_link():
    async def exercise():
        async with radio() as r:
            bridge = r.bridge
            bridge.address = OWN
            old, fresh = BackendClient(connected=True), BackendClient(connected=True)
            bridge._client = old
            with (
                patch.object(charger.asyncio, "sleep", r.h.clock.sleep),
                patch.object(bridge, "_async_post_session_sync", AsyncMock()),
            ):
                bridge._schedule_disconnect()
                await r.advance(1)
                old.is_connected = False
                bridge._handle_client_disconnect(old)
                bridge._client = fresh
                await r.advance(charger.CHARGER_IDLE_DISCONNECT_SECONDS)
                assert fresh.is_connected
                assert bridge._client is fresh

    asyncio.run(exercise())


def test_fresh_inactive_status_does_not_reuse_brush_run_from_old_link():
    async def exercise():
        async with radio() as r:
            bridge = r.bridge
            bridge.address = OWN
            old, fresh = BackendClient(connected=True), BackendClient(connected=True)
            bridge._client = old
            with patch.object(bridge, "_ensure_live_task"):
                await native_reply(bridge, old, ChargerCommand.BRUSH_STATUS, "33c004")
                assert bridge.session_running
                old.is_connected = False
                bridge._handle_client_disconnect(old)
                assert not bridge.session_running
                bridge._client = fresh
                await native_reply(bridge, fresh, ChargerCommand.SESSION_STATUS, "3ac000")
                assert not bridge.session_running

    asyncio.run(exercise())


def test_matched_charger_revalidates_on_observed_pairing_transition():
    async def exercise():
        async with radio() as r:
            bridge = r.bridge
            bridge.async_start()
            r.advert(OWN)
            await r.advance(1)
            assert bridge.address == OWN
            assert r.requests.count((OWN, ChargerCommand.BRUSH_DATA)) == 1
            r.advert(OWN, bits=0)
            await r.advance(1)
            r.pairs[OWN] = OTHER_BRUSH
            r.advert(OWN, bits=0x48)
            await r.advance(3)
            assert r.attempts_for(OWN) == [0, 2]
            assert r.requests.count((OWN, ChargerCommand.BRUSH_DATA)) == 2
            assert bridge.address is None

    asyncio.run(exercise())


def test_brief_direct_battery_read_cannot_overwrite_verified_charger_sample():
    async def exercise():
        async with brush() as h:
            c = h.c
            entered, release = asyncio.Event(), asyncio.Event()

            async def read(uuid):
                if uuid == const.CHAR_STATUS_BLOB:
                    entered.set()
                    await release.wait()
                    return b"\x17"
                return None

            h.read_hook = read
            c.async_start()
            await h._feed(2, protocol=7)
            assert entered.is_set()
            c.charger.address = OWN
            c._charger_discovered(c.charger)
            await c._async_apply_charger_passthrough("FF05", b"\x3f")
            assert c.data["battery"] == 63
            release.set()
            await h._ready()
            assert c.data["battery"] == 63
            assert c.data["battery_source"] == const.DATA_SOURCE_CHARGER

    asyncio.run(exercise())


def test_later_clock_can_finish_a_valid_record_from_same_matched_brush():
    async def exercise():
        async with radio() as r:
            bridge = r.bridge
            bridge.address = OWN
            bridge._client = Client(r, OWN, bridge._on_disconnect, None)
            bridge.parent.data["protocol_version"] = 7
            start = r.h.clock.utcnow() - timedelta(seconds=150)
            retained = _record(start, 60, FIRST_ID)

            async def first(short_uuid):
                return retained if short_uuid == "FF29" else None

            async def second(short_uuid):
                if short_uuid == "FF22":
                    return _native_time(r.h.clock.utcnow()).to_bytes(4, "little")
                return None

            with patch.object(bridge, "_async_passthrough", first):
                await bridge._async_post_session_sync(force=True)
            assert bridge.parent.data["last_session_start"] is None
            await r.advance(61)
            with patch.object(bridge, "_async_passthrough", second):
                await bridge._async_post_session_sync(force=True)
            assert bridge.parent.data["last_session_start"] == start
            assert bridge.parent.data["last_session_id"] == FIRST_ID
            assert bridge.parent.data["sessions_today"] == 1

    asyncio.run(exercise())


def test_passive_frame_cannot_end_direct_session_during_initial_reads():
    async def exercise():
        async with brush() as h:
            c = h.c
            c.mode = const.CONNECTION_MODE_LIVE
            c.charger = None
            entered, release = asyncio.Event(), asyncio.Event()
            notifications = {}

            async def subscribe(uuid, callback):
                notifications[uuid] = callback

            async def read(uuid):
                if uuid == const.CHAR_STATUS_BLOB:
                    entered.set()
                    await release.wait()
                return None

            h.client.start_notify = subscribe
            h.read_hook = read
            c.async_start()
            try:
                await h._feed(8, 0, protocol=7, connectable=True)
                assert entered.is_set()
                for uuid, sample in (
                    (const.CHAR_STATE, b"\x03"),
                    (const.CHAR_BRUSH_TIME, b"\x00\x0f"),
                ):
                    notifications[uuid](SimpleNamespace(uuid=uuid), bytearray(sample))
                assert c.data["state"] == "running"
                await h._feed(2, 0, protocol=7, connectable=True)
                assert c.data["state"] == "running"
                assert c.data["time"] == 15
            finally:
                release.set()
                await h._ready()

    asyncio.run(exercise())


@pytest.mark.parametrize("sample,expected", [(63, 63), (255, 23)])
def test_initial_read_yields_only_to_a_valid_newer_battery_notification(sample, expected):
    async def exercise():
        async with brush() as h:
            client = BackendClient(connected=True)
            notify = await direct_callback(h, client, const.CHAR_STATUS_BLOB)
            entered, release = asyncio.Event(), asyncio.Event()

            async def read(_client, uuid, label):
                if uuid == const.CHAR_STATUS_BLOB:
                    entered.set()
                    await release.wait()
                    return bytearray((23,))
                return None

            with patch.object(h.c, "_async_sync_read", read):
                initial = asyncio.create_task(h.c._async_initial_reads(client))
                try:
                    await entered.wait()
                    notify(SimpleNamespace(uuid=const.CHAR_STATUS_BLOB), bytearray((sample,)))
                finally:
                    release.set()
                    await initial
            assert h.c.data["battery"] == expected

    asyncio.run(exercise())


def test_active_matched_reconnect_stops_after_three_failed_attempts():
    async def exercise():
        async with radio() as r:
            bridge = r.bridge
            bridge.async_start()
            r.advert(OWN)
            await r.advance(1)
            old = Client(r, OWN, bridge._on_disconnect, None)
            bridge._client = old
            bridge._session_running = bridge._session_confirmed = True
            r.outcomes[OWN].extend(charger.BleakError("weak signal") for _ in range(4))
            old.is_connected = False
            bridge._handle_client_disconnect(old)
            await r.advance(60)
            assert bridge.address == OWN
            assert r.attempts_for(OWN) == [0, 6, 11, 16]
            assert old.released
            assert bridge.parent.data["live"] is False

    asyncio.run(exercise())


def test_reassociation_updates_the_same_logical_charger_device():
    async def exercise():
        async with radio() as r:
            c, bridge = r.h.c, r.bridge
            entry = MagicMock(entry_id="brush")
            c.hass.data = {const.DOMAIN: {entry.entry_id: c}}
            devices = MagicMock()
            entities = MagicMock()
            entities.async_get_entity_id.return_value = None
            batches, connected = [], []

            def subscribe(hass, signal, update):
                connected.append(update)
                return lambda: None

            with (
                patch.object(sensor.dr, "async_get", return_value=devices),
                patch.object(sensor.er, "async_get", return_value=entities),
                patch.object(sensor, "async_dispatcher_connect", side_effect=subscribe),
            ):
                await sensor.async_setup_entry(
                    c.hass, entry, lambda batch: batches.append(list(batch))
                )
                assert len(batches) == 1
                for address, firmware in ((OWN, "1.2.3"), ("02:00:00:00:00:23", "2.3.4")):
                    bridge.address = address
                    bridge.data["firmware"] = firmware
                    connected[0]()

            assert len(batches) == 2
            assert len(batches[1]) == len(sensor.CHARGER_SENSORS)
            charger_creates = [
                call.kwargs for call in devices.async_get_or_create.call_args_list[1:]
            ]
            assert [kwargs["sw_version"] for kwargs in charger_creates] == ["1.2.3", "2.3.4"]
            assert all(
                kwargs["identifiers"] == {(const.DOMAIN, f"{c.address}-iosense")}
                and "connections" not in kwargs
                for kwargs in charger_creates
            )

    asyncio.run(exercise())


def test_matched_active_link_recovers_live_timer_after_remote_loss():
    async def exercise():
        async with radio() as r:
            bridge = r.bridge
            r.brush_status[OWN] = 4
            r.brush_values[OWN, "FF08"] = b"\x00\x25"
            bridge.async_start()
            r.advert(OWN, bits=0x48)
            await r.advance(6)
            assert bridge.parent.data["live"]
            assert bridge.parent.data["time"] >= 37
            old = bridge._client

            r.brush_values[OWN, "FF08"] = b"\x00\x37"
            old.is_connected = False
            old.disconnected(old)
            await r.advance(12)

            assert old.released
            assert bridge._client is not old
            assert r.attempts_for(OWN) == [0, 11]
            assert bridge.parent.data["live"]
            assert bridge.parent.data["time"] >= 55
            assert bridge._retry_timer is None

    asyncio.run(exercise())


def test_idle_disconnect_keeps_last_sampled_charger_diagnostics():
    async def exercise():
        async with radio() as r:
            r.brush_status[OWN] = 2
            r.bridge.async_start()
            r.advert(OWN, bits=0x48)
            await r.advance(3)
            assert r.bridge.address == OWN
            assert r.bridge.available
            assert not r.bridge.data["bridge_connected"]
            assert r.bridge.data["brush_status"] == "idle"
            assert r.bridge.data["session_status"] == "inactive"

    asyncio.run(exercise())


def test_stale_native_active_status_does_not_start_session_before_brush_read():
    async def exercise():
        async with radio() as r:
            r.brush_status[OWN] = 2
            r.bridge.async_start()
            r.advert(OWN, bits=0x48)
            await r.advance(3)
            assert r.bridge.address == OWN
            assert not r.bridge.session_running
            assert not r.bridge.data["bridge_connected"]

            # The raw session status can lag the motor status. During the
            # second link it reports active while the brush reports idle.
            r.session_status[OWN] = 1
            r.response_delay = 0.85
            with patch.object(
                r.h.c, "_charger_session_started", wraps=r.h.c._charger_session_started
            ) as started:
                r.advert(OWN, bits=0x58)
                await r.advance(14)
                assert len(r.clients[OWN]) == 2
                started.assert_not_called()
                assert not r.bridge.session_running

    asyncio.run(exercise())


@pytest.mark.parametrize(
    ("characteristic", "label", "payload", "key"),
    [
        (const.CHAR_SMILEY, "smiley", b"\x07", "smiley_raw"),
        (const.CHAR_PRESSURE, "pressure", b"\x02", "pressure_raw"),
        (const.CHAR_BRUSH_TIME, "brushing time", b"\x01\x18", "time"),
    ],
)
def test_direct_startup_sample_cannot_overwrite_state_of_next_session(
    characteristic, label, payload, key
):
    async def exercise():
        async with brush() as h:
            c = h.c
            c.mode = const.CONNECTION_MODE_LIVE
            c.charger = None
            client = BackendClient(connected=True)
            await direct_callback(h, client, const.CHAR_STATE)
            entered, release = asyncio.Event(), asyncio.Event()

            def notify(uuid, value):
                c._on_notify(SimpleNamespace(uuid=uuid), bytearray(value))

            async def read(_client, uuid, read_label):
                if uuid == characteristic and read_label == label:
                    entered.set()
                    await release.wait()
                    return bytearray(payload)
                return None

            notify(const.CHAR_STATE, b"\x03")
            notify(const.CHAR_BRUSH_TIME, b"\x01\x12")
            with patch.object(c, "_async_sync_read", read):
                startup = asyncio.create_task(c._async_initial_reads(client))
                try:
                    await entered.wait()
                    generation = c._session_face_generation
                    notify(const.CHAR_STATE, b"\x02")
                    notify(const.CHAR_STATE, b"\x03")
                    # Confirm a distinct session without supplying a new
                    # notification for the characteristic under test.
                    c._track_session_time(0, confirm_session=True)
                    assert c._session_face_generation > generation
                    expected = c.data[key]
                finally:
                    release.set()
                    await startup
            assert c.data[key] == expected

    asyncio.run(exercise())


def test_native_session_status_remains_a_fallback_when_brush_status_times_out():
    async def exercise():
        async with radio() as r:
            r.brush_status[OWN] = None
            r.session_status[OWN] = 1
            r.bridge.async_start()
            r.advert(OWN, bits=0x48)
            await r.advance(6)
            assert r.bridge.address == OWN
            assert r.bridge._session_confirmed
            assert r.bridge.parent.data["live"]

    asyncio.run(exercise())


def test_unrelated_advertisement_does_not_cancel_active_link_recovery():
    async def exercise():
        async with radio() as r:
            bridge = r.bridge
            bridge.async_start()
            r.advert(OWN)
            await r.advance(1)
            assert bridge.address == OWN

            # This charger still advertises the original connection flags,
            # although an established transport reported the brush running.
            old = Client(r, OWN, bridge._on_disconnect, None)
            bridge._client = old
            bridge._session_running = bridge._session_confirmed = True
            old.is_connected = False
            old.disconnected(old)
            await r.advance(1)

            r.advert(OWN, firmware=5)
            await r.advance(5)
            assert r.attempts_for(OWN) == [0, 6]
            assert old.released

    asyncio.run(exercise())


def test_expired_scanner_history_does_not_interrupt_live_charger_link():
    async def exercise():
        async with radio() as r:
            c, bridge = r.h.c, r.bridge
            r.brush_status[OWN] = 4
            r.brush_values[OWN, "FF08"] = b"\x00\x25"
            bridge.async_start()
            r.advert(OWN, bits=0x48)
            await r.advance(6)
            client = bridge._client
            assert c.data["live"]
            assert client.is_connected

            # The manager has no scanner left claiming the address. An
            # already-open GATT client can still carry live data.
            r.h.manager._async_check_unavailable()
            assert r.h.manager.async_ble_device_from_address(OWN, True) is None
            r.brush_values[OWN, "FF08"] = b"\x00\x37"
            await r.advance(9)
            assert bridge._client is client
            assert client.is_connected
            assert bridge.available and c.available
            assert c.data["live"]
            assert c.data["time"] >= 55
            assert r.attempts_for(OWN) == [0]

            client.is_connected = False
            client.disconnected(client)
            await r.advance()
            assert client.released
            assert not bridge.available
            assert not c.available
            assert not c.data["live"]
            assert bridge.address == OWN

    asyncio.run(exercise())


@pytest.mark.parametrize("same_brush", [True, False])
def test_returning_charger_revalidates_pairing_even_if_advertisement_is_identical(same_brush):
    async def exercise():
        async with radio() as r:
            r.bridge.async_start()
            r.advert(OWN)
            await r.advance(1)
            assert r.bridge.address == OWN
            assert r.bridge.available
            r.h.manager._async_check_unavailable()
            assert not r.bridge.available
            if not same_brush:
                r.pairs[OWN] = OTHER_BRUSH

            r.advert(OWN)
            await r.advance(3)
            assert r.requests.count((OWN, ChargerCommand.BRUSH_DATA)) == 2
            assert r.bridge.address == (OWN if same_brush else None)
            assert r.bridge.available is same_brush

    asyncio.run(exercise())


def test_live_session_resumes_over_reconnected_charger_and_counts_once():
    async def exercise():
        async with radio() as r:
            c, bridge = r.h.c, r.bridge
            r.brush_status[OWN] = 4
            r.brush_values[OWN, "FF08"] = b"\x00\x25"
            r.brush_values[OWN, "FF07"] = b"\x01"
            bridge.async_start()
            r.advert(OWN, bits=0x48)
            await r.advance(7)
            first = bridge._client
            assert c.data["live"] and c._session_confirmed
            started = c._session_start
            generation = c._session_generation
            assert c.data["time"] >= 37

            r.brush_values[OWN, "FF08"] = b"\x00\x2e"
            first.is_connected = False
            first.disconnected(first)
            await r.advance(10)
            r.brush_values[OWN, "FF08"] = b"\x00\x31"
            await r.advance(5)
            assert bridge._client is not first
            assert first.released
            assert c.data["live"]
            assert c.data["time"] >= 49
            assert c._session_start == started
            assert c._session_generation == generation

            r.brush_status[OWN] = 2
            await r.advance(50)
            assert not c.data["live"]
            assert c.data["sessions_today"] == 1
            assert c.data["last_session_start"] == started
            assert c.data["last_session_duration"] >= 49

    asyncio.run(exercise())


@pytest.mark.parametrize("continuing", [True, False])
@pytest.mark.parametrize("lose_first_timer_reply", [False, True])
def test_slow_charger_reconnect_checks_timer_before_pause_grace_expires(
    continuing, lose_first_timer_reply
):
    async def exercise():
        async with radio() as r:
            c, bridge = r.h.c, r.bridge
            r.response_delay = 1.4
            r.brush_status[OWN] = 4
            r.brush_values[OWN, "FF07"] = b"\x01"
            r.brush_values[OWN, "FF08"] = lambda: bytes(divmod(30 + int(r.elapsed), 60))
            bridge.async_start()
            r.advert(OWN, bits=0x48)
            await r.advance(22)
            assert c.data["live"] and c.data["time"] >= 45
            generation, started = c._session_generation, c._session_start
            first = bridge._client
            r.brush_values[OWN, "FF05"] = b"\x3e"
            drop_next = lose_first_timer_reply

            def resumed_timer():
                nonlocal drop_next
                if drop_next:
                    drop_next = False
                    return None
                elapsed = int(r.elapsed)
                return bytes(divmod(30 + elapsed if continuing else max(0, elapsed - 28), 60))

            r.brush_values[OWN, "FF08"] = resumed_timer
            first.is_connected = False
            first.disconnected(first)

            await r.advance(29)
            assert c.data["live"]
            assert c.data["battery"] == 62
            if continuing:
                assert c.data["time"] >= 70
                assert c._session_start == started
                assert c._session_generation == generation
            else:
                assert c.data["time"] < 30
                assert c._session_start > started
                assert c._session_generation == generation + 1

            r.brush_status[OWN] = 2
            await r.advance(55)
            assert not c.data["live"]
            if continuing:
                assert c.data["sessions_today"] == 1
                assert c.data["last_session_start"] == started
            else:
                assert c.data["sessions_today"] == 2
                assert c.data["last_session_start"] > started

    asyncio.run(exercise())


@pytest.mark.parametrize("uuid", [const.CHAR_STATE, const.CHAR_SMILEY])
def test_direct_mode_can_recover_from_a_stalled_subscription(uuid):
    async def exercise():
        async with brush() as h:
            c = h.c
            c.mode = const.CONNECTION_MODE_LIVE
            c.charger = None
            stalled = False
            cancelled = []

            async def subscribe(characteristic, _callback):
                nonlocal stalled
                if characteristic == uuid and not stalled:
                    stalled = True
                    try:
                        await asyncio.Future()
                    finally:
                        cancelled.append(characteristic)

            h.client.start_notify = AsyncMock(side_effect=subscribe)
            c.async_start()
            await h._feed(2, connectable=True)
            assert stalled
            await h._advance(6)

            assert cancelled == [uuid]
            if uuid == const.CHAR_STATE:
                assert not h.client.is_connected
                assert not c.data["live"]
                await h._advance(const.RECONNECT_INTERVAL_SECONDS)
                assert len(h.connections) == 2
            else:
                assert len(h.connections) == 1
            assert h.client.is_connected
            assert c.data["live"]
            assert c.data["battery"] == 23

    asyncio.run(exercise())


def test_stalled_startup_value_cannot_hold_direct_mode_in_setup():
    async def exercise():
        async with brush() as h:
            c = h.c
            c.mode = const.CONNECTION_MODE_LIVE
            c.charger = None
            cancelled = []

            async def read(uuid):
                if uuid == const.CHAR_STATUS_BLOB:
                    try:
                        await asyncio.Future()
                    finally:
                        cancelled.append(uuid)
                if uuid == const.CHAR_BRUSH_TIME:
                    return b"\x00\x0f"
                return None

            h.read_hook = read
            c.async_start()
            await h._feed(2, connectable=True)
            await h._advance(const.SYNC_READ_TIMEOUT_SECONDS + 1)
            assert cancelled == [const.CHAR_STATUS_BLOB]
            assert h.client.is_connected
            assert c.data["live"]
            assert c.data["time"] == 15

    asyncio.run(exercise())


@pytest.mark.parametrize("during_other_request", [False, True])
def test_unmatched_native_reply_cannot_start_charger_session(during_other_request):
    async def exercise():
        async with radio() as r:
            bridge = r.bridge
            bridge.address = OWN
            client = BackendClient(connected=True)
            bridge._client = client

            def delayed_status():
                bridge._on_read(None, bytearray.fromhex("33c004"))

            async def write(_uuid, payload, *, response):
                if payload == charger.IOSENSE_PROTOCOL_END:
                    delayed_status()
                    bridge._on_read(None, bytearray.fromhex("03c0696f"))

            client.write_gatt_char = write
            with patch.object(bridge, "_ensure_live_task"):
                if during_other_request:
                    reply = await bridge._async_get(ChargerCommand.DEVICE_ID)
                    assert reply.value == "io"
                else:
                    delayed_status()
                assert bridge.data["brush_status"] is None
                assert not bridge.session_running
                assert not bridge.parent.data["live"]

    asyncio.run(exercise())


def test_second_native_reply_cannot_change_status_after_request_is_satisfied():
    async def exercise():
        async with radio() as r:
            bridge = r.bridge
            bridge.address = OWN
            client = BackendClient(connected=True)
            bridge._client = client

            async def write(_uuid, payload, *, response):
                if payload == charger.IOSENSE_PROTOCOL_END:
                    bridge._on_read(None, bytearray.fromhex("33c000"))
                    bridge._on_read(None, bytearray.fromhex("33c004"))

            client.write_gatt_char = write
            with patch.object(bridge, "_ensure_live_task"):
                reply = await bridge._async_get(ChargerCommand.BRUSH_STATUS)
                assert reply.value == "not_connected"
                assert bridge.data["brush_status"] == "not_connected"
                assert not bridge.session_running

    asyncio.run(exercise())


@pytest.mark.parametrize("command,invalid,valid", [
    (ChargerCommand.BRUSH_STATUS, "33c0", "33c004"),
    (ChargerCommand.SESSION_STATUS, "3ac0", "3ac001"),
])
def test_empty_status_reply_leaves_request_open_for_actual_status(command, invalid, valid):
    async def exercise():
        async with radio() as r:
            bridge = r.bridge
            bridge.address = OWN
            client = BackendClient(connected=True)
            bridge._client = client

            async def write(_uuid, payload, *, response):
                if payload == charger.IOSENSE_PROTOCOL_END:
                    bridge._on_read(None, bytearray.fromhex(invalid))
                    bridge._on_read(None, bytearray.fromhex(valid))

            client.write_gatt_char = write
            with patch.object(bridge, "_ensure_live_task"):
                reply = await bridge._async_get(command)
                assert reply.payload == bytes.fromhex(valid)[2:]
                assert bridge.session_running

    asyncio.run(exercise())


@pytest.mark.parametrize("route_available", [False, True])
def test_live_charger_silence_releases_link_and_recovers_only_when_reachable(route_available):
    async def exercise():
        async with radio() as r:
            c, bridge = r.h.c, r.bridge
            r.brush_status[OWN] = 4
            r.brush_values[OWN, "FF08"] = b"\x00\x25"
            bridge.async_start()
            r.advert(OWN, bits=0x48)
            await r.advance(7)
            old = bridge._client
            assert old.is_connected and c.data["live"]
            generation = c._session_generation
            r.drop_replies = lambda client: client is old
            if not route_available:
                r.h.manager._async_check_unavailable()
            await r.advance(8)
            assert old.is_connected and c.data["live"], "allow a short outage"
            await r.advance(11)
            assert old.released
            assert c._pending_session.duration == 37
            if not route_available:
                assert not bridge.available and not c.data["live"]
                assert r.attempts_for(OWN) == [0]
                return

            r.brush_values[OWN, "FF08"] = b"\x00\x37"
            await r.advance(8)
            assert bridge._client is not old and c.data["live"]
            r.brush_values[OWN, "FF08"] = b"\x00\x3a"
            await r.advance(5)
            assert c._session_generation == generation
            r.brush_status[OWN] = 2
            await r.advance(45)
            assert c.data["sessions_today"] == 1

    asyncio.run(exercise())


def test_silent_reconnects_use_existing_budget_without_polling_snapshot():
    async def exercise():
        async with radio() as r:
            bridge = r.bridge
            r.brush_status[OWN] = 4
            bridge.async_start()
            r.advert(OWN, bits=0x48)
            await r.advance(5)
            old = bridge._client
            assert old.is_connected and bridge.session_running
            r.drop_replies = lambda _client: True
            old.is_connected = False
            old.disconnected(old)
            await r.advance(26)
            assert r.attempts_for(OWN) == [0, 10, 18, 26]
            assert bridge._async_snapshot.await_count == 0
            assert all(client.released for client in r.clients[OWN])
            assert not bridge.session_running and not bridge.parent.data["live"]

    asyncio.run(exercise())


def test_silent_initial_status_probe_retries_without_a_new_advertisement():
    async def exercise():
        async with radio() as r:
            bridge = r.bridge
            r.brush_status[OWN] = 4
            r.brush_values[OWN, "FF08"] = b"\x00\x25"
            r.drop_replies = lambda client: (
                client is r.clients[OWN][0] and client.command != ChargerCommand.BRUSH_DATA
            )
            bridge.async_start()
            r.advert(OWN, bits=0x48)
            await r.advance(7)
            assert bridge.address == OWN
            assert r.clients[OWN][0].released
            assert not bridge.session_running
            await r.advance(10)
            assert r.attempts_for(OWN) == [0, 8]
            assert bridge.parent.data["live"]
            assert bridge.parent.data["time"] >= 37

    asyncio.run(exercise())


@pytest.mark.parametrize("status_replies", [False, True])
@pytest.mark.parametrize("passthrough_times_out", [False, True])
def test_failed_passthrough_needs_live_status_to_keep_session(status_replies, passthrough_times_out):
    async def exercise():
        async with radio() as r:
            bridge = r.bridge
            r.response_delay = 1.4
            r.brush_status[OWN] = 4
            r.brush_values[OWN, "FF08"] = b"\x00\x25"
            bridge.async_start()
            r.advert(OWN, bits=0x48)
            await r.advance(25)
            old = bridge._client
            assert bridge.session_running
            r.brush_values.clear()
            if passthrough_times_out:
                r.drop_replies = lambda client: client.command == ChargerCommand.BRUSH_PASSTHROUGH
            if not status_replies:
                r.brush_status[OWN] = None
            await r.advance(45)
            assert old.released is not status_replies
            assert (bridge._client is old) is status_replies

    asyncio.run(exercise())


@pytest.mark.parametrize("passthrough", [False, True])
def test_reply_before_command_delimiter_cannot_satisfy_new_request(passthrough):
    async def exercise():
        async with radio() as r:
            bridge = r.bridge
            client = BackendClient(connected=True)
            bridge._client = client
            if passthrough:
                header = bytes.fromhex("c137")
                old = "37c105ff01010117"
                current = "37c105ff0101013f"
            else:
                header = bytes.fromhex("c039")
                old = "39c000112233445566"
                current = "39c000aabbccddeeff"

            async def write(uuid, payload, *, response):
                if payload == header or uuid == charger.IOSENSE_WRITE_UUID:
                    bridge._on_read(None, bytearray.fromhex(old))
                if payload == charger.IOSENSE_PROTOCOL_END:
                    bridge._on_read(None, bytearray.fromhex(current))

            client.write_gatt_char = write
            if passthrough:
                assert await bridge._async_passthrough("FF05") == b"\x3f"
            else:
                reply = await bridge._async_get(ChargerCommand.BRUSH_DATA)
                assert reply.value["brush_mac"] == "AA:BB:CC:DD:EE:FF"

    asyncio.run(exercise())
