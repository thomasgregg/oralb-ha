"""Issue #58 regression tests for iO Sense charger discovery.

Home Assistant's Bluetooth manager replays and deduplicates advertisements.
A virtual clock drives real retry callbacks and request deadlines. The radio
and GATT replies are simulated. There is no physical Bluetooth access.
"""

from __future__ import annotations

import asyncio
from collections import defaultdict, deque
from contextlib import ExitStack, asynccontextmanager
from itertools import permutations
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import habluetooth.manager as bluetooth_manager
import pytest

from tests.test_advertisement_callbacks import service_info
from tests.test_battery_refresh import ADDRESS, _ModuleProxy, const, coordinator
from tests.test_post_session_beta import brush

from oralb_live import charger
from oralb_live.charger_protocol import IOSENSE_SERVICE_UUID, ChargerCommand


OWN = "C0:5D:89:E7:96:92"
NEIGHBOR = "70:4B:CA:50:18:46"
THIRD = "00:11:22:33:44:77"
OTHER_BRUSH = "11:22:33:44:55:66"
READ_TIMEOUT = 1.5


def identity(mac: str) -> bytes:
    """Build a BRUSH_DATA notification with the MAC at its wire offset."""
    return bytes((ChargerCommand.BRUSH_DATA, 0xC0, 0)) + bytes.fromhex(
        mac.replace(":", "")
    )


class Client:
    def __init__(self, radio, address, disconnected, outcome):
        self.radio = radio
        self.address = address
        self.disconnected = disconnected
        self.outcome = outcome
        self.is_connected = outcome != "already_disconnected"
        self.notifications = {}
        self.command = None
        self.short_uuid = None
        self.released = False
        self.cancelled_notifications = []

    async def start_notify(self, uuid, callback):
        if self.outcome == "notify_error":
            raise charger.BleakError("notification subscription failed")
        if self.outcome == ("stall_notify", uuid):
            try:
                await asyncio.Future()
            finally:
                self.cancelled_notifications.append(uuid)
        if self.radio.notification_delay:
            await self.radio.h.clock.sleep(self.radio.notification_delay)
        self.notifications[uuid] = callback
        if self.outcome == "lost_during_notify":
            self.is_connected = False
            self.disconnected(self)

    async def disconnect(self):
        self.is_connected = False
        self.released = True

    def reply(self, payload):
        self.notifications[charger.IOSENSE_READ_UUID](None, bytearray(payload))

    async def write_gatt_char(self, uuid, payload, *, response):
        if uuid == charger.IOSENSE_WRITE_UUID:
            self.short_uuid = f"{payload[1]:02X}{payload[0]:02X}"
            return
        if payload != charger.IOSENSE_PROTOCOL_END:
            self.command = ChargerCommand(payload[1])
            return
        if self.radio.response_delay:
            await self.radio.h.clock.sleep(self.radio.response_delay)
        self.radio.requests.append((self.address, self.command))
        if self.radio.drop_replies(self):
            return
        if self.command == ChargerCommand.BRUSH_DATA:
            if self.outcome == "silent":
                return
            if isinstance(self.outcome, bytes):
                self.reply(self.outcome)
            else:
                self.reply(identity(self.radio.pairs[self.address]))
        elif self.command == ChargerCommand.BRUSH_STATUS:
            status = self.radio.brush_status[self.address]
            if status is not None:
                self.reply(bytes((self.command, 0xC0, status)))
        elif self.command == ChargerCommand.SESSION_STATUS:
            self.reply(bytes((self.command, 0xC0, self.radio.session_status[self.address])))
        elif self.command == ChargerCommand.BRUSH_PASSTHROUGH:
            data = self.radio.brush_values.get((self.address, self.short_uuid), b"")
            if callable(data):
                data = data()
            if data is None:
                return
            high, low = bytes.fromhex(self.short_uuid)
            self.reply(
                bytes((self.command, 0xC1, low, high, 1, bool(data), len(data))) + data
            )


class Radio:
    """Keep the bridge, request parser and advertisement manager under test."""

    def __init__(self, harness):
        self.h = harness
        self.attempts = []
        self.lookups = []
        self.requests = []
        self.clients = defaultdict(list)
        self.outcomes = defaultdict(deque)
        self.pairs = {OWN: ADDRESS, NEIGHBOR: OTHER_BRUSH}
        self.brush_status = defaultdict(int)
        self.session_status = defaultdict(int)
        self.brush_values = {}
        self.response_delay = 0
        self.notification_delay = 0
        self.drop_replies = lambda _client: False
        self.route_down = set()
        self.tasks_blocked_at_connect = {}
        self.bridges = [harness.c.charger]
        self.jitter = 1.0
        self.proxy_slots = None
        self.rejected_by_proxy = 0

    @property
    def bridge(self):
        return self.bridges[0]

    @property
    def elapsed(self):
        return self.h.clock.now - 1000.0

    def advert(self, address, *, bits=0x40, firmware=4, rssi=-88):
        mac = bytes.fromhex(address.replace(":", ""))
        payload = bytes((2, 0xA2, 0, 3, firmware, 2)) + mac + bytes((1, bits))
        info = service_info(
            address, self.h.clock.now, payload, connectable=True,
            service_uuids=(IOSENSE_SERVICE_UUID,), rssi=rssi,
        )
        self.h.manager.scanner_adv_received(info)
        return info

    def add_bridge(self, address):
        parent = coordinator.OralBLiveCoordinator(
            self.h.hass, address, "Second brush", const.CONNECTION_MODE_CHARGER
        )
        parent.charger._async_snapshot = AsyncMock()
        self.bridges.append(parent.charger)
        return parent.charger

    def lookup(self, hass, address, connectable=True):
        self.lookups.append((address, self.elapsed, connectable))
        if address in self.route_down:
            return None
        return self.h.manager.async_ble_device_from_address(address, connectable)

    async def connect(self, cls, device, name, *, disconnected_callback, **kwargs):
        address = device.address
        self.attempts.append((address, self.elapsed))
        if (gate := self.tasks_blocked_at_connect.get(address)) is not None:
            await gate.wait()
        outcome = self.outcomes[address].popleft() if self.outcomes[address] else None
        if isinstance(outcome, Exception):
            raise outcome
        if self.proxy_slots is not None:
            used = sum(
                not client.released
                for clients in self.clients.values()
                for client in clients
            )
            if used >= self.proxy_slots:
                self.rejected_by_proxy += 1
                raise charger.BleakError("proxy has no connection slot available")
        client = Client(self, address, disconnected_callback, outcome)
        self.clients[address].append(client)
        return client

    def attempts_for(self, address):
        return [when for candidate, when in self.attempts if candidate == address]

    async def advance(self, seconds=0):
        await self.h._advance(seconds)


@asynccontextmanager
async def radio():
    async with brush() as harness:
        simulated = Radio(harness)
        loop = asyncio.get_running_loop()
        harness.manager._loop = loop
        harness.hass.loop.time = lambda: harness.clock.now
        harness.hass.loop.call_soon_threadsafe = loop.call_soon_threadsafe
        with ExitStack() as patches:
            patches.enter_context(patch.object(
                bluetooth_manager, "monotonic_time_coarse",
                side_effect=lambda: harness.clock.now,
            ))
            patches.enter_context(patch.object(
                charger, "asyncio",
                _ModuleProxy(asyncio, timeout=harness.clock.timeout, sleep=harness.clock.sleep),
            ))
            patches.enter_context(patch.object(
                charger, "time", _ModuleProxy(charger.time, monotonic=lambda: harness.clock.now),
            ))
            if hasattr(charger, "random"):
                patches.enter_context(patch.object(
                    charger.random, "uniform", side_effect=lambda *_: simulated.jitter,
                ))
            patches.enter_context(patch.object(
                charger.bluetooth, "async_ble_device_from_address", simulated.lookup,
            ))
            patches.enter_context(patch.object(charger, "establish_connection", simulated.connect))
            patches.enter_context(patch.object(charger, "async_dispatcher_send"))
            patches.enter_context(patch.object(simulated.bridge, "_async_snapshot", AsyncMock()))
            try:
                yield simulated
            finally:
                for bridge in simulated.bridges:
                    await bridge.async_stop()


@pytest.mark.parametrize("limited_proxy", [False, True])
def test_cached_two_charger_replay_matches_both_brushes_without_changed_advertising(limited_proxy):
    async def exercise():
        async with radio() as r:
            first, second = r.bridge, r.add_bridge(OTHER_BRUSH)
            if limited_proxy:
                r.proxy_slots = 1
            r.advert(NEIGHBOR)
            r.advert(OWN)
            with patch.object(first, "_async_advertisement", wraps=first._async_advertisement) as receive:
                first.async_start()
                second.async_start()
                await r.advance(1)
                assert receive.call_count == 2
                r.advert(OWN)
                r.advert(NEIGHBOR)
                assert receive.call_count == 2, "unchanged advertisements must be suppressed"
                await r.advance(90 if limited_proxy else 4)
                assert (first.address, second.address) == (OWN, NEIGHBOR)
                if limited_proxy:
                    assert r.rejected_by_proxy > 0
                assert first.parent.data["charger_address"] == OWN
                assert second.parent.data["charger_address"] == NEIGHBOR
                assert receive.call_count == 2

    asyncio.run(exercise())


@pytest.mark.parametrize(
    "failure",
    [
        "route_missing", "connect_error", "connect_timeout", "already_disconnected",
        "notify_error", "lost_during_notify", "silent", "truncated",
    ],
)
def test_transient_failure_recovers_without_another_advertisement(failure):
    async def exercise():
        async with radio() as r:
            outcomes = {
                "connect_error": charger.BleakError("weak signal"),
                "connect_timeout": TimeoutError(),
                "truncated": bytes.fromhex("39c000aabb"),
            }
            if failure == "route_missing":
                r.route_down.add(OWN)
            else:
                r.outcomes[OWN].append(outcomes.get(failure, failure))
            r.bridge.async_start()
            r.advert(OWN)
            await r.advance(READ_TIMEOUT)
            assert r.bridge.address is None
            assert all(client.released for client in r.clients[OWN])
            r.route_down.discard(OWN)
            due = 15 + (READ_TIMEOUT if failure == "silent" else 0)
            await r.advance(due - r.elapsed - 0.01)
            assert r.bridge.address is None
            await r.advance(0.02)
            assert r.bridge.address == OWN
            assert [address for address, _, _ in r.lookups] == [OWN, OWN]
            assert all(connectable for _, _, connectable in r.lookups)

    asyncio.run(exercise())


@pytest.mark.parametrize("placeholder", ["00:00:00:00:00:00", "FF:FF:FF:FF:FF:FF"])
def test_non_device_mac_does_not_permanently_exclude_candidate(placeholder):
    async def exercise():
        async with radio() as r:
            r.outcomes[OWN].append(identity(placeholder))
            r.bridge.async_start()
            r.advert(OWN)
            await r.advance(16)
            assert r.bridge.address == OWN
            assert r.attempts_for(OWN) == [0, 15]

    asyncio.run(exercise())


def test_confirmed_foreign_charger_is_excluded_without_further_radio_attempts():
    async def exercise():
        async with radio() as r:
            r.bridge.async_start()
            r.advert(NEIGHBOR)
            await r.advance(1)
            r.advert(NEIGHBOR, firmware=5)
            await r.advance(360)
            assert r.bridge.address is None
            assert r.attempts_for(NEIGHBOR) == [0]
            assert r.clients[NEIGHBOR][0].released

    asyncio.run(exercise())


def test_second_candidate_starts_its_own_backoff():
    async def exercise():
        async with radio() as r:
            r.outcomes[OWN].append(charger.BleakError("first failure on A"))
            r.outcomes[NEIGHBOR].append(charger.BleakError("first failure on B"))
            r.pairs[NEIGHBOR] = ADDRESS
            r.bridge.async_start()
            r.advert(OWN)
            await r.advance(1)
            r.advert(NEIGHBOR)
            r.outcomes[OWN].append(charger.BleakError("A still unavailable"))
            await r.advance(15)
            assert r.attempts_for(NEIGHBOR) == [1, 16]
            assert r.bridge.address == NEIGHBOR

    asyncio.run(exercise())


@pytest.mark.parametrize("other_connect_in_progress", [False, True])
def test_changed_advertisements_cannot_shorten_a_failure_backoff(other_connect_in_progress):
    async def exercise():
        async with radio() as r:
            r.outcomes[OWN].append(charger.BleakError("first failure"))
            r.bridge.async_start()
            r.advert(OWN)
            await r.advance(1)
            gate = asyncio.Event()
            if other_connect_in_progress:
                r.tasks_blocked_at_connect[NEIGHBOR] = gate
                r.advert(NEIGHBOR)
                await r.advance()
            for revision in range(5, 8):
                r.advert(OWN, firmware=revision)
                await r.advance(1)
            gate.set()
            await r.advance(10.9)
            assert r.attempts_for(OWN) == [0]
            await r.advance(2.1)
            assert r.attempts_for(OWN) == [0, 15]
            assert r.bridge.address == OWN

    asyncio.run(exercise())


def test_jittered_retry_delay_is_capped_at_five_minutes():
    async def exercise():
        async with radio() as r:
            r.jitter = 1.2
            r.outcomes[OWN].extend(charger.BleakError("weak signal") for _ in range(8))
            r.bridge.async_start()
            r.advert(OWN)
            await r.advance(858)
            assert r.attempts_for(OWN) == [0, 18, 54, 126, 270, 558, 858]

    asyncio.run(exercise())


def test_unverified_foreign_charger_does_not_mark_brush_available():
    async def exercise():
        async with radio() as r:
            r.bridge.async_start()
            r.advert(NEIGHBOR)
            await r.advance(1)
            assert r.bridge.address is None
            assert (
                r.bridge.available, r.bridge.parent.available, r.bridge.data["address"]
            ) == (False, False, None)

    asyncio.run(exercise())


@pytest.mark.parametrize("own_active", [False, True])
def test_matching_uses_its_own_cached_advertisement_when_another_arrives(own_active):
    async def exercise():
        async with radio() as r:
            r.bridge.async_start()
            r.advert(OWN, bits=0x48 if own_active else 0x40, rssi=-62)
            r.advert(NEIGHBOR, bits=0x40 if own_active else 0x48, rssi=-91)
            await r.advance(6)
            assert r.bridge.address == OWN
            status_read = (OWN, ChargerCommand.BRUSH_STATUS) in r.requests
            assert (r.bridge.data["mac"], r.bridge.data["rssi"], status_read) == (
                OWN, -62, own_active,
            )

    asyncio.run(exercise())


def test_late_identity_reply_from_previous_client_cannot_match_new_client():
    async def exercise():
        async with radio() as r:
            r.outcomes[OWN].append("silent")
            r.outcomes[NEIGHBOR].append("silent")
            r.bridge.async_start()
            r.advert(OWN)
            await r.advance(READ_TIMEOUT)
            old = r.clients[OWN][0]
            assert old.released
            r.advert(NEIGHBOR)
            await r.advance()
            new = r.clients[NEIGHBOR][0]
            # Deliver the old valid reply while the new request is outstanding,
            # followed by the new client's real, foreign identity.
            old.reply(identity(ADDRESS))
            await r.advance()
            new.reply(identity(OTHER_BRUSH))
            await r.advance()
            assert r.bridge.address is None
            assert new.released

    asyncio.run(exercise())


@pytest.mark.parametrize("during_request", [False, True])
def test_stop_cancels_pending_retry_and_releases_clients(during_request):
    async def exercise():
        async with radio() as r:
            r.outcomes[OWN].append("silent")
            r.bridge.async_start()
            r.advert(OWN)
            r.advert(NEIGHBOR)
            await r.advance(0.5 if during_request else READ_TIMEOUT)
            first_client = r.clients[OWN][0]
            await r.bridge.async_stop()
            r.advert(OWN, firmware=5)
            await r.advance(360)
            assert r.bridge.address is None
            assert r.attempts == [(OWN, 0)]
            assert first_client.released
            assert r.bridge._pending_future is None
            assert not r.bridge._request_lock.locked()

    asyncio.run(exercise())


def test_match_cancels_other_pending_discoveries():
    async def exercise():
        async with radio() as r:
            r.outcomes[NEIGHBOR].append(charger.BleakError("intermittent neighbor"))
            r.bridge.async_start()
            r.advert(NEIGHBOR)
            r.advert(OWN)
            await r.advance(4)
            assert r.bridge.address == OWN
            r.advert(NEIGHBOR, firmware=5)
            await r.advance(360)
            assert r.attempts == [(NEIGHBOR, 0), (OWN, 2)]
            assert r.bridge.data["mac"] == OWN

    asyncio.run(exercise())


def test_confirmed_foreign_charger_can_be_rediscovered_after_observed_repair():
    async def exercise():
        async with radio() as r:
            r.bridge.async_start()
            r.advert(NEIGHBOR)
            await r.advance(1)
            assert r.bridge.address is None
            r.advert(NEIGHBOR, bits=0)
            await r.advance(1)
            r.pairs[NEIGHBOR] = ADDRESS
            r.advert(NEIGHBOR, bits=0x40)
            await r.advance(16)
            assert r.bridge.address == NEIGHBOR

    asyncio.run(exercise())


@pytest.mark.parametrize("eager", [False, True])
def test_simultaneous_due_candidates_are_serialized_during_slow_identification(eager):
    async def exercise():
        if eager:
            asyncio.get_running_loop().set_task_factory(asyncio.eager_task_factory)
        async with radio() as r:
            first_gate, second_gate = asyncio.Event(), asyncio.Event()
            r.tasks_blocked_at_connect.update({NEIGHBOR: first_gate, THIRD: second_gate})
            r.pairs[THIRD] = OTHER_BRUSH
            r.bridge.async_start()
            r.advert(NEIGHBOR)
            r.advert(THIRD)
            r.advert(OWN)
            await r.advance(5)
            assert r.attempts == [(NEIGHBOR, 0)]
            first_gate.set()
            await r.advance(2)
            assert r.attempts == [(NEIGHBOR, 0), (THIRD, 5)]
            second_gate.set()
            await r.advance(5)
            assert r.bridge.address == OWN
            assert [candidate for candidate, _ in r.attempts] == [NEIGHBOR, THIRD, OWN]

    asyncio.run(exercise())


def test_matched_charger_keeps_existing_reconnect_policy():
    async def exercise():
        async with radio() as r:
            r.bridge.async_start()
            r.advert(OWN)
            await r.advance(1)
            assert r.bridge.address == OWN
            r.outcomes[OWN].append(charger.BleakError("matched charger temporarily unreachable"))
            r.advert(OWN, bits=0x48)
            await r.advance(360)
            assert r.attempts_for(OWN) == [0, 1]
            assert r.bridge.address == OWN
            r.advert(OWN)
            await r.advance()
            assert r.attempts_for(OWN) == [0, 1, 361]
            assert r.bridge.address == OWN

    asyncio.run(exercise())


def test_reported_restart_restores_live_brushing_over_weak_shared_proxy():
    async def exercise():
        async with radio() as r:
            r.proxy_slots = 1
            r.response_delay = 0.85
            r.brush_status[OWN] = 4  # A running handle connected to its charger.
            r.brush_values.update(
                {
                    (OWN, "FF05"): b"\x47",
                    (OWN, "FF07"): b"\x01",
                    (OWN, "FF08"): b"\x00\x25",
                    (OWN, "FF09"): b"\x02\x05\x04",
                    (OWN, "FF0B"): b"\x02",
                }
            )
            # Both cached charger advertisements are replayed at registration.
            # The first belongs to the other entry and the frames stay unchanged.
            r.advert(NEIGHBOR)
            r.advert(OWN, bits=0x48)
            second = r.add_bridge(OTHER_BRUSH)
            r.h.c.async_start()
            second.async_start()
            await r.h._feed(8, protocol=9)  # Handle only advertises selection_menu.
            assert r.h.c.data["state"] == "selection_menu"
            await r.advance(45)

            assert (r.bridge.address, second.address) == (OWN, NEIGHBOR)
            assert r.rejected_by_proxy >= 1
            assert r.h.c.data["state"] == "running"
            assert r.h.c.data["data_source"] == const.DATA_SOURCE_CHARGER
            assert r.h.c.data["live"]
            assert r.h.c.data["time"] >= 37
            assert r.h.c.data["sector"] == "sector_3"
            assert r.h.c.data["pressure"] == "high"
            assert r.h.c.data["battery"] == 71
            assert (NEIGHBOR, ChargerCommand.BRUSH_PASSTHROUGH) not in r.requests

    asyncio.run(exercise())


def test_expired_candidate_pauses_until_advertised_again():
    async def exercise():
        async with radio() as r:
            r.route_down.add(OWN)
            r.bridge.async_start()
            r.advert(OWN)
            await r.advance(1)
            assert len(r.lookups) == 1
            with patch.object(charger.bluetooth, "async_last_service_info", return_value=None):
                await r.advance(360)
            assert len(r.lookups) == 1
            r.route_down.remove(OWN)
            r.advert(OWN, firmware=5)
            await r.advance()
            assert r.bridge.address == OWN

    asyncio.run(exercise())


def test_pairing_change_during_identity_read_requires_a_new_identity():
    async def exercise():
        async with radio() as r:
            r.bridge.async_start()
            r.outcomes[OWN].append("silent")
            r.advert(OWN)
            await r.advance()
            stale = r.clients[OWN][0]
            r.advert(OWN, bits=0)
            r.pairs[OWN] = OTHER_BRUSH
            r.advert(OWN, bits=0x40)
            stale.reply(identity(ADDRESS))
            await r.advance(4)
            assert r.attempts_for(OWN) == [0, 0]
            assert r.bridge.address is None
            assert stale.released
            assert r.clients[OWN][1].released

    asyncio.run(exercise())


@pytest.mark.parametrize("order", list(permutations((OWN, NEIGHBOR, THIRD))))
@pytest.mark.parametrize("proxy_slots", (1, 2))
@pytest.mark.parametrize("response_delay", (0.85, 1.4))
def test_three_bridges_recover_from_proxy_contention_and_missing_route(
    order, proxy_slots, response_delay
):
    async def exercise():
        async with radio() as r:
            r.proxy_slots = proxy_slots
            r.response_delay = response_delay
            third_brush = "02:00:00:00:00:42"
            r.pairs[THIRD] = third_brush
            bridges = (r.bridge, r.add_bridge(OTHER_BRUSH), r.add_bridge(third_brush))
            r.route_down.add(OWN)
            for address in order:
                r.advert(address)
            for bridge in bridges:
                bridge.async_start()

            await r.advance(7)
            r.route_down.remove(OWN)
            # Route recovery arrives without any new advertisements.
            await r.advance(293)
            assert tuple(bridge.address for bridge in bridges) == (OWN, NEIGHBOR, THIRD)
            assert r.rejected_by_proxy > 0
            assert all(client.released for clients in r.clients.values() for client in clients)
            assert all(bridge._retry_timer is None for bridge in bridges)

    asyncio.run(exercise())


@pytest.mark.parametrize("uuid", [charger.IOSENSE_READ_UUID, charger.IOSENSE_STATUS_UUID])
def test_stalled_notification_setup_cannot_hold_the_discovery_queue(uuid):
    async def exercise():
        async with radio() as r:
            r.proxy_slots = 1
            r.outcomes[NEIGHBOR].append(("stall_notify", uuid))
            r.bridge.async_start()
            r.advert(NEIGHBOR)
            r.advert(OWN)
            await r.advance(10)

            assert r.bridge.address == OWN
            assert r.attempts == [(NEIGHBOR, 0), (OWN, 5)]
            assert r.clients[NEIGHBOR][0].cancelled_notifications == [uuid]
            assert r.clients[NEIGHBOR][0].released
            assert r.rejected_by_proxy == 0

    asyncio.run(exercise())


def test_timed_out_notification_setup_retries_the_same_candidate():
    async def exercise():
        async with radio() as r:
            r.outcomes[OWN].append(("stall_notify", charger.IOSENSE_STATUS_UUID))
            r.bridge.async_start()
            r.advert(OWN)
            await r.advance(19)
            assert r.bridge.address is None
            await r.advance(2)
            assert r.attempts_for(OWN) == [0, 20]
            assert r.bridge.address == OWN
            assert r.clients[OWN][0].released

    asyncio.run(exercise())


def test_responsive_charger_gets_a_separate_deadline_for_each_subscription():
    async def exercise():
        async with radio() as r:
            r.notification_delay = 4.5
            r.bridge.async_start()
            r.advert(OWN)
            await r.advance(10)
            assert r.bridge.address == OWN
            assert r.attempts_for(OWN) == [0]
            client = r.clients[OWN][0]
            assert client.cancelled_notifications == []
            assert client.released

    asyncio.run(exercise())


def test_foreign_charger_can_be_rediscovered_after_its_route_disappears():
    async def exercise():
        async with radio() as r:
            r.bridge.async_start()
            r.advert(NEIGHBOR)
            await r.advance(1)
            assert r.bridge.address is None
            assert r.attempts_for(NEIGHBOR) == [0]
            r.h.manager._async_check_unavailable()
            assert r.h.manager.async_ble_device_from_address(NEIGHBOR, True) is None

            # The charger was paired again while away. The paired flag and
            # every other advertisement byte are identical on return.
            r.pairs[NEIGHBOR] = ADDRESS
            r.advert(NEIGHBOR)
            await r.advance(3)
            assert r.attempts_for(NEIGHBOR) == [0, 1]
            assert r.bridge.address == NEIGHBOR

    asyncio.run(exercise())


@pytest.mark.parametrize("after_loss", ["absent", "returned_before_reply", "returned_before_timeout"])
def test_lost_route_invalidates_pending_identity_and_preserves_returning_candidate(after_loss):
    async def exercise():
        async with radio() as r:
            r.outcomes[OWN].append("silent")
            r.bridge.async_start()
            r.advert(OWN)
            await r.advance(0.25)
            stale = r.clients[OWN][0]
            assert r.requests == [(OWN, ChargerCommand.BRUSH_DATA)]
            r.h.manager._async_check_unavailable()
            assert OWN not in r.bridge._candidates
            r.pairs[OWN] = OTHER_BRUSH
            if after_loss != "absent":
                r.advert(OWN)
            if after_loss != "returned_before_timeout":
                stale.reply(identity(ADDRESS))
            await r.advance(3)
            assert stale.released
            expected_attempts = [0] if after_loss == "absent" else [0, 2.25]
            assert r.attempts_for(OWN) == expected_attempts
            assert r.requests == [(OWN, ChargerCommand.BRUSH_DATA)] * len(expected_attempts)
            assert r.bridge.address is None
            assert r.bridge.parent.data["charger_address"] is None

    asyncio.run(exercise())


def test_unload_unregisters_foreign_candidate_availability_tracker():
    async def exercise():
        async with radio() as r:
            r.bridge.async_start()
            r.advert(NEIGHBOR)
            await r.advance(1)
            watchers = r.h.manager._connectable_unavailable_callbacks
            assert len(watchers[NEIGHBOR]) == 1
            await r.bridge.async_stop()
            assert NEIGHBOR not in watchers
            r.h.manager._async_check_unavailable()
            r.advert(NEIGHBOR)
            await r.advance(3)
            assert r.attempts_for(NEIGHBOR) == [0]

    asyncio.run(exercise())
