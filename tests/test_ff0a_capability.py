"""Issue #45 contracts for an absent optional FF0A characteristic.

Exercise both production capture paths with the existing virtual clock and
session tracker. Only transport/service discovery is simulated. An absent
FF0A may be probed once, but must not consume the remaining retry budget on
that same validated service table. Generic errors are not proof of absence.
"""

import asyncio
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from bleak.backends.service import BleakGATTServiceCollection

from tests import test_post_session_beta as harness

const = harness.const
coordinator = harness.coordinator


def services(*, face=True, core=True):
    """Use bleak's real lookup container with a small discovered GATT table."""
    result = BleakGATTServiceCollection()
    chars = []
    service = SimpleNamespace(
        handle=1,
        uuid="a0f03e00-5047-4d53-8208-4f72616c2d42",
        characteristics=chars,
        add_characteristic=chars.append,
    )
    result.add_service(service)
    uuids = [const.CHAR_STATUS_BLOB]
    if core:
        uuids.append(const.CHAR_STATE)
    if face:
        uuids.append(const.CHAR_SMILEY)
    for handle, uuid in enumerate(uuids, 2):
        result.add_characteristic(SimpleNamespace(
            handle=handle, uuid=uuid, service_handle=1,
            service_uuid=service.uuid, properties=["read"],
        ))
    return result


def missing_face():
    # Match the actual protocol-7 / firmware-36 hardware report. A generic
    # BleakError is also what the minimum supported bleak version exposes.
    return coordinator.BleakError(f"Characteristic {const.CHAR_SMILEY} was not found!")


def face_reads(h):
    return [at for at, uuid in h.reads if uuid == const.CHAR_SMILEY]


async def stop_session(h, path):
    if path == "quiet":
        await h._stop_session(face=0)
        return
    h.c.mode = const.CONNECTION_MODE_LIVE
    h.c._schedule_connect = lambda: None
    h.c._client = h.client
    h.client.is_connected = True
    h.c.data["data_source"] = const.DATA_SOURCE_DIRECT
    # Deliver real state/timer callbacks; keep production grace/retry delays.
    for uuid, payload in (
        (const.CHAR_STATE, b"\x03"),
        (const.CHAR_BRUSH_TIME, b"\x02\x00"),
        (const.CHAR_STATE, b"\x02"),
    ):
        h.c._on_notify(SimpleNamespace(uuid=uuid), bytearray(payload))
    await h._ready()


@pytest.mark.parametrize("path", ["quiet", "direct"])
def test_confirmed_missing_ff0a_stops_retries_and_repeated_failure_logs(path, caplog):
    async def exercise():
        async with harness.brush() as h:
            h.client.services = services(face=False)
            h.responses[const.CHAR_SMILEY] = missing_face()
            with caplog.at_level("DEBUG", logger=coordinator.__name__):
                await stop_session(h, path)
                await h._advance(4)
            failures = [r for r in caplog.records if "read failed" in r.message
                        and const.CHAR_SMILEY in r.message]
            assert len(face_reads(h)) <= 1, "confirmed absent FF0A was read repeatedly"
            assert len(failures) <= 1, "missing FF0A produced repeated failure logs"
            await h._advance(20)
            assert h.c.data["last_session_display_face"] is None
            assert h.c.data["last_session_display_face_source"] is None
            assert h.c.data["sessions_today"] == 1
            if path == "quiet":
                h._assert_published_battery(23)
                assert h.c.data["battery_source"] == const.DATA_SOURCE_DIRECT

    asyncio.run(exercise())


@pytest.mark.parametrize("path", ["quiet", "direct"])
@pytest.mark.parametrize("failure", ["generic", "timeout", "off", "empty"])
def test_transient_failure_or_no_result_keeps_supported_handle_retries(path, failure):
    async def exercise():
        async with harness.brush() as h:
            h.client.services = services()
            first = {
                "generic": coordinator.BleakError("temporary read failure"),
                "timeout": TimeoutError(), "off": b"\x00", "empty": b"",
            }[failure]
            attempts = 0

            async def read(uuid):
                nonlocal attempts
                if uuid == const.CHAR_STATUS_BLOB:
                    return b"\x17"
                if uuid == const.CHAR_SMILEY:
                    attempts += 1
                    if attempts == 1:
                        if isinstance(first, Exception):
                            raise first
                        return first
                    return b"\x03"
                return None

            h.read_hook = read
            await stop_session(h, path)
            await h._advance(20)
            assert len(face_reads(h)) == 2
            assert face_reads(h)[1] - face_reads(h)[0] == 0.5
            assert h.c.data["last_session_display_face"] == "special_3"
            assert h.c.data["last_session_display_face_source"] == const.DATA_SOURCE_DIRECT
            if path == "quiet":
                h._assert_published_battery(23)

    asyncio.run(exercise())


@pytest.mark.parametrize("path", ["quiet", "direct"])
@pytest.mark.parametrize(
    "discovery", ["lazy", "incomplete", "lookup-error", "no-lookup", "noncallable-lookup"]
)
def test_unreliable_service_discovery_does_not_disable_face_capture(path, discovery):
    async def exercise():
        async with harness.brush() as h:
            if discovery == "lazy":
                h.client.services = None
            elif discovery == "incomplete":
                h.client.services = services(face=False, core=False)
            elif discovery == "no-lookup":
                h.client.services = SimpleNamespace()
            elif discovery == "noncallable-lookup":
                h.client.services = SimpleNamespace(get_characteristic=None)
            else:
                def lookup(uuid):
                    raise coordinator.BleakError("service discovery not ready")
                h.client.services = SimpleNamespace(get_characteristic=lookup)
            attempts = 0

            async def read(uuid):
                nonlocal attempts
                if uuid == const.CHAR_STATUS_BLOB:
                    return b"\x17"
                if uuid == const.CHAR_SMILEY:
                    attempts += 1
                    if attempts == 1:
                        # A stale/incomplete table is not a validated absence.
                        raise missing_face()
                    h.client.services = services()
                    return b"\x01"
                return None

            h.read_hook = read
            await stop_session(h, path)
            await h._advance(20)
            assert h.c.data["last_session_display_face"] == "standard"
            assert len(face_reads(h)) == 2

    asyncio.run(exercise())


@pytest.mark.parametrize("path", ["quiet", "direct"])
@pytest.mark.parametrize("change", ["same-client-new-services", "replacement-client"])
def test_absence_is_reconsidered_after_services_or_client_are_rebuilt(path, change):
    async def exercise():
        async with harness.brush() as h:
            h.client.services = services(face=False)
            h.responses[const.CHAR_SMILEY] = missing_face()
            await stop_session(h, path)
            await h._advance(4)
            old_reads = len(face_reads(h))
            # Rediscovery during the still-open result window must permit a
            # new read; no coordinator/model-wide permanent blacklist.
            if change == "replacement-client":
                old = h.client
                new = MagicMock()
                new.is_connected = True
                new.services = services()
                new.read_gatt_char = old.read_gatt_char
                new.disconnect = old.disconnect
                h.client = new
                if path == "direct":
                    h.c._client = new
            else:
                h.client.services = services()
            h.responses[const.CHAR_SMILEY] = b"\x04"
            if path == "quiet":
                task = asyncio.create_task(h.c._async_quiet_display_face(h.client))
            else:
                task = asyncio.create_task(h.c._async_read_session_display_face(
                    h.c._pending_session_face_generation, h.client
                ))
            await h._ready()
            await task
            await h._advance(20)
            assert len(face_reads(h)) == old_reads + 1
            assert h.c.data["last_session_display_face"] == "special_4"

    asyncio.run(exercise())


@pytest.mark.parametrize("path", ["quiet", "direct"])
def test_absent_face_cannot_inherit_previous_sessions_result(path):
    async def exercise():
        async with harness.brush() as h:
            h.client.services = services(face=False)
            h.c.data["last_session_display_face"] = "special_7"
            h.c.data["last_session_display_face_source"] = const.DATA_SOURCE_DIRECT
            h.responses[const.CHAR_SMILEY] = missing_face()
            await stop_session(h, path)
            await h._advance(20)
            assert h.c.data["last_session_display_face"] is None
            assert h.c.data["last_session_display_face_source"] is None
            assert h.c.data["last_session_duration"] == 120
            if path == "quiet":
                h._assert_published_battery(23)

    asyncio.run(exercise())


def test_missing_optional_face_preserves_battery_and_later_record_recovery():
    async def exercise():
        async with harness.brush() as h:
            h.client.services = services(face=False)
            h.responses[const.CHAR_SMILEY] = missing_face()
            h.responses[const.CHAR_SESSION_DATA] = harness.fixtures._record()
            h.responses[const.CHAR_RTC] = (1120).to_bytes(4, "little")
            await stop_session(h, "quiet")
            h._assert_published_battery(23)
            assert h.c._session_pending_sync
            assert const.CHAR_SESSION_DATA not in [uuid for _, uuid in h.reads]
            await h._advance(60)
            await h._feed(2, 120)
            await h._advance(4)
            assert const.CHAR_SESSION_DATA in [uuid for _, uuid in h.reads]
            assert not h.c._session_pending_sync
            assert h.c.data["sessions_today"] == 1
            assert h.c.data["battery"] == 23
            assert h.c.data["battery_source"] == const.DATA_SOURCE_DIRECT
            assert h.c.data["last_session_display_face"] is None

    asyncio.run(exercise())


@pytest.mark.parametrize("path", ["quiet", "direct"])
def test_delayed_face_read_from_old_session_cannot_attach_after_new_session(path):
    async def exercise():
        async with harness.brush() as h:
            h.client.services = services()
            release = asyncio.Event()
            entered = asyncio.Event()

            async def read(uuid):
                if uuid == const.CHAR_STATUS_BLOB:
                    return b"\x17"
                if uuid == const.CHAR_SMILEY:
                    entered.set()
                    await release.wait()
                    return b"\x07"
                return None

            h.read_hook = read
            await stop_session(h, path)
            assert entered.is_set()
            if path == "quiet":
                await h._feed(3, 0)
            else:
                h.c._on_notify(SimpleNamespace(uuid=const.CHAR_STATE), bytearray(b"\x03"))
                h.c._on_notify(SimpleNamespace(uuid=const.CHAR_BRUSH_TIME), bytearray(b"\x00\x00"))
            release.set()
            await h._ready()
            await h._advance(4)
            assert h.c.data["last_session_display_face"] is None
            assert h.c._session_base_display_face is None

    asyncio.run(exercise())


def test_known_absence_is_reused_for_same_live_client_and_service_table():
    async def exercise():
        async with harness.brush() as h:
            h.client.services = services(face=False)
            h.responses[const.CHAR_SMILEY] = missing_face()
            await stop_session(h, "direct")
            await h._advance(4)
            task = asyncio.create_task(h.c._async_read_session_display_face(
                h.c._pending_session_face_generation, h.client
            ))
            await h._advance(4)
            await task
            assert len(face_reads(h)) <= 1, "same service table was probed repeatedly"

    asyncio.run(exercise())


@pytest.mark.parametrize("path", ["quiet", "direct"])
@pytest.mark.parametrize(
    "case", ["generic", "other-characteristic", "face-present", "timeout-message"]
)
def test_uncorroborated_or_misleading_error_keeps_retry_budget(path, case):
    async def exercise():
        async with harness.brush() as h:
            h.client.services = services(face=case == "face-present")
            error = {
                "generic": coordinator.BleakError("Device was not found!"),
                "other-characteristic": coordinator.BleakError(
                    f"Characteristic {const.CHAR_STATUS_BLOB} was not found!"
                ),
                "face-present": missing_face(),
                "timeout-message": TimeoutError(str(missing_face())),
            }[case]
            attempts = 0

            async def read(uuid):
                nonlocal attempts
                if uuid == const.CHAR_STATUS_BLOB:
                    return b"\x17"
                if uuid == const.CHAR_SMILEY:
                    attempts += 1
                    if attempts == 1:
                        raise error
                    return b"\x03"
                return None

            h.read_hook = read
            await stop_session(h, path)
            await h._advance(20)
            assert len(face_reads(h)) == 2
            assert h.c.data["last_session_display_face"] == "special_3"

    asyncio.run(exercise())


def add_face(table):
    table.add_characteristic(SimpleNamespace(
        handle=99, uuid=const.CHAR_SMILEY, service_handle=1,
        service_uuid="a0f03e00-5047-4d53-8208-4f72616c2d42", properties=["read"],
    ))


@pytest.mark.parametrize("path", ["quiet", "direct"])
def test_in_place_service_update_invalidates_previous_absence(path):
    async def exercise():
        async with harness.brush() as h:
            table = services(face=False)
            h.client.services = table
            h.responses[const.CHAR_SMILEY] = missing_face()
            await stop_session(h, path)
            await h._advance(4)
            assert len(face_reads(h)) == 1
            add_face(table)
            h.responses[const.CHAR_SMILEY] = b"\x04"
            if path == "quiet":
                await h.c._async_quiet_display_face(h.client)
            else:
                await h.c._async_read_session_display_face(
                    h.c._pending_session_face_generation, h.client
                )
            await h._advance(20)
            assert len(face_reads(h)) == 2
            assert h.c.data["last_session_display_face"] == "special_4"

    asyncio.run(exercise())


@pytest.mark.parametrize("path", ["quiet", "direct"])
@pytest.mark.parametrize("change", ["replace", "mutate"])
def test_service_change_during_missing_read_does_not_poison_new_discovery(path, change):
    async def exercise():
        async with harness.brush() as h:
            h.client.services = services(face=False)
            entered = asyncio.Event()
            release = asyncio.Event()
            attempts = 0

            async def read(uuid):
                nonlocal attempts
                if uuid == const.CHAR_STATUS_BLOB:
                    return b"\x17"
                if uuid == const.CHAR_SMILEY:
                    attempts += 1
                    if attempts == 1:
                        entered.set()
                        await release.wait()
                        raise missing_face()
                    return b"\x01"
                return None

            h.read_hook = read
            await stop_session(h, path)
            assert entered.is_set()
            if change == "replace":
                # Even an equally shaped table is a new discovery generation.
                h.client.services = services(face=False)
            else:
                add_face(h.client.services)
            release.set()
            await h._ready()
            await h._advance(20)
            assert len(face_reads(h)) == 2
            assert h.c.data["last_session_display_face"] == "standard"

    asyncio.run(exercise())


def test_reconnect_reusing_client_and_service_objects_discards_negative_evidence():
    async def exercise():
        async with harness.brush() as h:
            h.client.services = services(face=False)
            h.responses[const.CHAR_SMILEY] = missing_face()
            await stop_session(h, "direct")
            await h._advance(4)
            assert len(face_reads(h)) == 1
            # Exercise the real establish helper instead of the fixture shortcut.
            with patch.object(coordinator, "establish_connection", return_value=h.client):
                result = await coordinator.OralBLiveCoordinator._async_establish_brush_connection(
                    h.c, object(), max_attempts=1
                )
            assert result is h.client
            h.responses[const.CHAR_SMILEY] = b"\x01"
            await h.c._async_read_session_display_face(
                h.c._pending_session_face_generation, result
            )
            await h._advance(20)
            assert len(face_reads(h)) == 2
            assert h.c.data["last_session_display_face"] == "standard"

    asyncio.run(exercise())


def test_initial_read_missing_face_does_not_repeat_in_post_session_capture():
    async def exercise():
        async with harness.brush() as h:
            h.client.services = services(face=False)
            h.client.is_connected = True
            h.responses[const.CHAR_SMILEY] = missing_face()
            await h.c._async_initial_reads(h.client)
            assert len(face_reads(h)) == 1
            await stop_session(h, "direct")
            await h._advance(20)
            assert len(face_reads(h)) == 1
            assert h.c.data["last_session_display_face"] is None
            h._assert_published_battery(23)

    asyncio.run(exercise())


def test_real_ff0a_notification_invalidates_previous_missing_table_evidence():
    async def exercise():
        async with harness.brush() as h:
            h.client.services = services(face=False)
            h.responses[const.CHAR_SMILEY] = missing_face()
            await stop_session(h, "direct")
            await h._advance(4)
            assert len(face_reads(h)) == 1
            # Even an off result proves FF0A exists; a stale service table
            # must not prevent a later valid read on this connection.
            h.c._on_notify(SimpleNamespace(uuid=const.CHAR_SMILEY), bytearray(b"\x00"))
            h.responses[const.CHAR_SMILEY] = b"\x01"
            await h.c._async_read_session_display_face(
                h.c._pending_session_face_generation, h.client
            )
            await h._advance(20)
            assert len(face_reads(h)) == 2
            assert h.c.data["last_session_display_face"] == "standard"

    asyncio.run(exercise())
