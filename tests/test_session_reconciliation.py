"""Issue-39 follow-up: one brushing must survive delayed FF29 recovery once.

These are acceptance tests for beta 3, intentionally run against beta 2 first.
They exercise the real advertisement manager, scheduler, record parser, session
tracker and sensor restoration. Only Bluetooth transport, storage and time use
the existing deterministic fixtures; production settlement/cooldown delays stay
unchanged. Unusable clock samples must not invent another session or its date.
"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from zoneinfo import ZoneInfo

import pytest

from tests import test_battery_refresh as fixtures

const = fixtures.const
coordinator = fixtures.coordinator
sensor = fixtures.sensor
NATIVE_EPOCH = 1_000_000
FIRST_ID = 3000
SECOND_ID = 3001
CLOCK_CASES = (
    "valid", "missing", "empty", "short", "zero", "before_start",
    "before_end", "error", "timeout",
)


def _native_time(wall_time: datetime) -> int:
    """A synthetic brush clock independent of the passively estimated start."""
    return NATIVE_EPOCH + int((wall_time - fixtures.WALL_START).total_seconds())


def _record(start: datetime, duration: int, session_id: int = SECOND_ID) -> bytes:
    raw = bytearray(fixtures._record(
        timestamp=_native_time(start), duration=duration, battery=94,
    ))
    raw[4:6] = session_id.to_bytes(2, "little")
    raw[16:18] = b"\x00\x00"  # Zero pressure events are a valid result.
    raw[19] = 0  # Match the actual daily-clean advertisements in the fixture.
    return bytes(raw)


@asynccontextmanager
async def _brush():
    h = fixtures.BatteryRefreshTests()
    await h.asyncSetUp()
    # A real reload must see saved record identity, not an always-empty mock.
    saved = {}

    async def load():
        return dict(saved)

    async def save(data):
        saved.clear()
        saved.update(data)

    h.c._store.async_load = AsyncMock(side_effect=load)
    h.c._store.async_save = AsyncMock(side_effect=save)
    try:
        h.c.async_start()
        yield h
    finally:
        await h.asyncTearDown()


async def _complete(h, duration=139, *, first_observed=0, face=3):
    """Perform a physical brushing, possibly first observed late by HA."""
    start = h.clock.utcnow()
    await h._advance(first_observed)
    await h._feed(3, first_observed)
    await h._advance(duration - first_observed)
    await h._feed(9, duration, face=face)
    await h._advance(20)  # Actual pause grace; no direct finalizer shortcut.
    return start


async def _two_sessions(h, *, second_duration=139, first_observed=0):
    first = await _complete(h)
    assert h.c.data["sessions_today"] == 1
    # Starts exactly 40 minutes apart, with coherent native clock timestamps.
    await h._advance(2400 - (h.clock.utcnow() - first).total_seconds())
    second = await _complete(
        h, second_duration, first_observed=first_observed, face=6,
    )
    assert h.c.data["sessions_today"] == 2
    return first, second


async def _recover(h, raw_record, clock_case="valid", *, clock_offset=0):
    """Trigger a real quiet-state sync and observe an actual FF29/FF22 read."""
    blocked = asyncio.Event()
    timestamp = int.from_bytes(raw_record[:4], "little")
    duration = int.from_bytes(raw_record[8:10], "little")

    async def read(uuid):
        if uuid == const.CHAR_STATUS_BLOB:
            return h.status
        if uuid == const.CHAR_SESSION_DATA:
            return raw_record
        if uuid != const.CHAR_RTC:
            return None
        if clock_case == "valid":
            return (_native_time(h.clock.utcnow()) + clock_offset).to_bytes(4, "little")
        if clock_case == "missing":
            return None
        if clock_case == "empty":
            return b""
        if clock_case == "short":
            return b"\x01\x02\x03"
        if clock_case == "zero":
            return bytes(4)
        if clock_case == "before_start":
            return (timestamp - 1).to_bytes(4, "little")
        if clock_case == "before_end":
            return (timestamp + duration - 1).to_bytes(4, "little")
        if clock_case == "error":
            raise coordinator.BleakError("FF22 read failed")
        if clock_case == "timeout":
            await blocked.wait()
        raise AssertionError(f"Unexpected clock case: {clock_case}")

    # The first summary connection reads battery promptly. This later quiet
    # observation comes after both the record settlement and slot cooldown.
    await h._advance(60)
    h.read_hook = read
    checkpoint = len(h.reads)
    quiet = 2 if h.c.data["state_raw"] != 2 else 4
    await h._feed(quiet, duration)
    reads = [uuid for _, uuid in h.reads[checkpoint:]]
    assert const.CHAR_SESSION_DATA in reads, "Scenario never read FF29"
    assert const.CHAR_RTC in reads, "Scenario never attempted the brush clock"
    assert reads[0] == const.CHAR_STATUS_BLOB
    assert h.c.data["battery"] == h.status[0]
    assert h.c.data["battery_source"] == const.DATA_SOURCE_DIRECT
    if clock_case == "timeout":
        # Battery must be visible while the RTC await is still blocked.
        assert h.client.is_connected
        await h._advance(const.SYNC_READ_TIMEOUT_SECONDS + 0.1)
    await h._ready()
    assert not h.client.is_connected


async def _reload(h, restore_order=("last_session", "sessions_today", "battery")):
    """Rebuild the coordinator and restore via the real sensor lifecycle."""
    states = {}
    for key in restore_order:
        description = next(item for item in sensor.SENSORS if item.key == key)
        entity = sensor.OralBLiveSensor(h.c, description)
        with patch.object(entity, "async_write_ha_state"):
            entity._handle_update(h.c.data)
        value = entity.native_value
        states[key] = SimpleNamespace(
            state=value.isoformat() if isinstance(value, datetime) else str(value),
            attributes=dict(entity.extra_state_attributes),
        )
    old_store = h.c._store
    await h.c.async_stop()
    h.c = coordinator.OralBLiveCoordinator(
        h.hass, fixtures.ADDRESS, "test", const.CONNECTION_MODE_CHARGER,
    )
    h.c._store.async_load = old_store.async_load
    h.c._store.async_save = old_store.async_save
    h.c._async_establish_brush_connection = AsyncMock(side_effect=h._connect)
    for key in restore_order:
        description = next(item for item in sensor.SENSORS if item.key == key)
        entity = sensor.OralBLiveSensor(h.c, description)
        entity.hass = h.hass
        entity.async_get_last_state = AsyncMock(return_value=states[key])
        with (
            patch.object(sensor, "async_dispatcher_connect", return_value=lambda: None),
            patch.object(entity, "async_write_ha_state"),
        ):
            await entity.async_added_to_hass()
    h.c.async_start()


@pytest.mark.parametrize("clock_case", CLOCK_CASES)
def test_matching_record_preserves_count_start_and_face(clock_case):
    async def exercise():
        async with _brush() as h:
            _, second = await _two_sessions(h)
            await _recover(h, _record(second, 139), clock_case)
            assert h.c.data["sessions_today"] == 2
            assert h.c.data["last_session_start"] == second
            assert h.c.data["last_session_duration"] == 139
            assert h.c.data["last_session_display_face"] == "special_6"
            assert h.c.data["battery"] == 23
    asyncio.run(exercise())


@pytest.mark.parametrize("first_observed", (119, 120, 121, 130))
def test_late_first_advertisement_still_matches_the_actual_session(first_observed):
    async def exercise():
        async with _brush() as h:
            _, second = await _two_sessions(h, first_observed=first_observed)
            await _recover(h, _record(second, 139))
            assert h.c.data["sessions_today"] == 2
            assert h.c.data["last_session_start"] == second
            assert h.c.data["last_session_duration"] == 139
            assert h.c.data["last_session_display_face"] == "special_6"
    asyncio.run(exercise())


@pytest.mark.parametrize("delay", (180, 600, 3600))
def test_delayed_recovery_without_clock_does_not_create_a_new_start(delay):
    async def exercise():
        async with _brush() as h:
            _, second = await _two_sessions(h)
            await h._advance(delay)
            await _recover(h, _record(second, 139), "missing")
            assert h.c.data["sessions_today"] == 2
            assert h.c.data["last_session_start"] == second
            assert h.c.data["last_session_display_face"] == "special_6"
    asyncio.run(exercise())


@pytest.mark.parametrize("clock_case", ("valid", "missing", "error"))
def test_already_recovered_record_is_idempotent_on_later_reads(clock_case):
    async def exercise():
        async with _brush() as h:
            _, second = await _two_sessions(h)
            raw = _record(second, 139)
            await _recover(h, raw)
            assert h.c.data["sessions_today"] == 2
            # Exercise a periodic refresh, not a manually invoked decoder.
            h.clock.now += const.PERIODIC_SYNC_INTERVAL_SECONDS
            await h._advance(0)
            await _recover(h, raw, clock_case)
            assert h.c.data["sessions_today"] == 2
            assert h.c.data["last_session_start"] == second
            assert h.c.data["last_session_id"] == SECOND_ID
    asyncio.run(exercise())


@pytest.mark.parametrize("clock_case", ("valid", "missing"))
def test_older_record_cannot_recount_or_replace_latest_same_duration_session(clock_case):
    async def exercise():
        async with _brush() as h:
            first, second = await _two_sessions(h)
            await _recover(h, _record(first, 139, FIRST_ID), clock_case)
            assert h.c.data["sessions_today"] == 2
            assert h.c.data["last_session_start"] == second
            assert h.c.data["last_session_display_face"] == "special_6"
            assert h.c.data["last_session_id"] != FIRST_ID
    asyncio.run(exercise())


def test_two_equal_duration_brushings_with_distinct_records_count_twice():
    async def exercise():
        async with _brush() as h:
            first = await _complete(h)
            await _recover(h, _record(first, 139, FIRST_ID))
            assert h.c.data["sessions_today"] == 1
            assert h.c.data["last_session_id"] == FIRST_ID
            await h._advance(2400 - (h.clock.utcnow() - first).total_seconds())
            second = await _complete(h, face=6)
            await _recover(h, _record(second, 139, SECOND_ID))
            assert h.c.data["sessions_today"] == 2
            assert h.c.data["last_session_start"] == second
            assert h.c.data["last_session_id"] == SECOND_ID
            assert h.c.data["last_session_display_face"] == "special_6"
    asyncio.run(exercise())


@pytest.mark.parametrize("clock_case", ("valid", "missing"))
@pytest.mark.parametrize("count_restored_first", (False, True))
def test_reload_before_record_recovery_does_not_recount(clock_case, count_restored_first):
    async def exercise():
        async with _brush() as h:
            _, second = await _two_sessions(h)
            order = ("sessions_today", "last_session", "battery") if count_restored_first else (
                "last_session", "sessions_today", "battery",
            )
            await _reload(h, order)
            assert h.c.data["sessions_today"] == 2
            assert h.c.data["last_session_start"] == second
            await _recover(h, _record(second, 139), clock_case)
            assert h.c.data["sessions_today"] == 2
            assert h.c.data["last_session_start"] == second
            assert h.c.data["last_session_display_face"] == "special_6"
    asyncio.run(exercise())


def test_saved_record_identity_survives_reload_and_missing_clock():
    async def exercise():
        async with _brush() as h:
            _, second = await _two_sessions(h)
            raw = _record(second, 139)
            await _recover(h, raw)
            assert h.c.data["sessions_today"] == 2
            await _reload(h)
            await _recover(h, raw, "missing")
            assert h.c.data["sessions_today"] == 2
            assert h.c.data["last_session_start"] == second
            assert h.c.data["last_session_id"] == SECOND_ID
    asyncio.run(exercise())


@pytest.mark.parametrize("zone", ("UTC", "Europe/Berlin", "America/New_York"))
@pytest.mark.parametrize("clock_case", ("valid", "missing"))
@pytest.mark.parametrize("crosses_midnight", (False, True))
def test_recovery_after_local_midnight_keeps_session_on_its_physical_day(
    zone, clock_case, crosses_midnight,
):
    async def exercise():
        async with _brush() as h:
            tz = ZoneInfo(zone)
            with patch.object(coordinator.dt_util.original, "DEFAULT_TIME_ZONE", tz):
                start = datetime(2026, 10, 1, 23, 59 if crosses_midnight else 55, tzinfo=tz)
                h.clock.now = 1000 + (
                    start.astimezone(timezone.utc) - fixtures.WALL_START
                ).total_seconds()
                physical_start = await _complete(h)
                await h._advance(600)
                h.c._async_reset_sessions_today(coordinator.dt_util.now())
                assert h.c.data["sessions_today"] == 0
                await _recover(h, _record(physical_start, 139), clock_case)
                assert h.c.data["sessions_today"] == 0
                assert h.c.data["last_session_start"] == physical_start
                assert h.c.sessions_today_date == coordinator.dt_util.now().date()
    asyncio.run(exercise())


@pytest.mark.parametrize("clock_case", ("valid", "missing"))
def test_pause_continuation_and_retained_record_remain_one_session(clock_case):
    async def exercise():
        async with _brush() as h:
            start = h.clock.utcnow()
            await h._feed(3, 0)
            await h._advance(70)
            await h._feed(2, 70, face=3)
            await h._advance(5)
            await h._feed(3, 70)
            await h._advance(1)
            await h._feed(3, 71)
            await h._advance(50)
            await h._feed(9, 121, face=6)
            await h._advance(20)
            assert h.c.data["sessions_today"] == 1
            await _recover(h, _record(start, 121), clock_case)
            assert h.c.data["sessions_today"] == 1
            assert h.c.data["last_session_start"] == start
            assert h.c.data["last_session_duration"] == 121
            assert h.c.data["last_session_display_face"] == "special_6"
    asyncio.run(exercise())


def test_battery_publishes_before_clock_timeout_and_is_not_replaced_by_record():
    async def exercise():
        async with _brush() as h:
            _, second = await _two_sessions(h)
            h.status = b"\x11"  # 17%, deliberately different from both earlier sources.
            await _recover(h, _record(second, 139), "timeout")
            latest = [p for p in h.publications if p.get("battery") == 17]
            assert latest
            assert latest[0]["battery_source"] == const.DATA_SOURCE_DIRECT
            assert h.c.data["battery"] == 17
            assert h.c.data["battery_source"] == const.DATA_SOURCE_DIRECT
            assert h.c.data["battery_updated_at"] == latest[0]["battery_updated_at"]
    asyncio.run(exercise())


def test_valid_retained_only_session_is_recovered_and_counted():
    async def exercise():
        async with _brush() as h:
            start = h.clock.utcnow() - timedelta(seconds=199)
            await _recover(h, _record(start, 139))
            assert h.c.data["sessions_today"] == 1
            assert h.c.data["last_session_start"] == start
            assert h.c.data["last_session_duration"] == 139
            assert h.c.data["last_session_id"] == SECOND_ID
            assert h.c.data["last_session_source"] == const.DATA_SOURCE_SESSION
    asyncio.run(exercise())


def test_later_valid_clock_can_resolve_a_previously_unusable_record():
    async def exercise():
        async with _brush() as h:
            _, second = await _two_sessions(h)
            raw = _record(second, 139)
            await _recover(h, raw, "missing")
            assert h.c.data["sessions_today"] == 2
            assert h.c.data["last_session_start"] == second
            h.clock.now += const.PERIODIC_SYNC_INTERVAL_SECONDS
            await h._advance(0)
            await _recover(h, raw, "valid")
            assert h.c.data["sessions_today"] == 2
            assert h.c.data["last_session_start"] == second
            assert h.c.data["last_session_id"] == SECOND_ID
            assert h.c.data["last_session_source"] == const.DATA_SOURCE_SESSION
    asyncio.run(exercise())


def test_unobserved_second_brushing_with_equal_duration_is_still_recovered():
    async def exercise():
        async with _brush() as h:
            first = await _complete(h)
            await _recover(h, _record(first, 139, FIRST_ID))
            assert h.c.data["sessions_today"] == 1
            await h._advance(2400 - (h.clock.utcnow() - first).total_seconds())
            second = h.clock.utcnow()
            # The motor advertisements were missed, but the summary announces
            # a new session. Only the later FF29/FF22 pair can reconstruct it.
            await h._advance(139)
            h.read_hook = None
            await h._feed(9, 139, face=0)
            assert h.c.data["sessions_today"] == 1
            await _recover(h, _record(second, 139, SECOND_ID))
            assert h.c.data["sessions_today"] == 2
            assert h.c.data["last_session_start"] == second
            assert h.c.data["last_session_id"] == SECOND_ID
    asyncio.run(exercise())


@pytest.mark.parametrize("reload_before_replay", (False, True))
@pytest.mark.parametrize("clock_case", ("valid", "missing"))
def test_replaying_previously_recovered_older_record_is_idempotent(
    reload_before_replay, clock_case,
):
    async def exercise():
        async with _brush() as h:
            first = await _complete(h)
            old_record = _record(first, 139, FIRST_ID)
            await _recover(h, old_record)
            await h._advance(2400 - (h.clock.utcnow() - first).total_seconds())
            second = await _complete(h, face=6)
            await _recover(h, _record(second, 139, SECOND_ID))
            assert h.c.data["sessions_today"] == 2
            if reload_before_replay:
                await _reload(h)
            h.clock.now += const.PERIODIC_SYNC_INTERVAL_SECONDS
            await h._advance(0)
            await _recover(h, old_record, clock_case)
            assert h.c.data["sessions_today"] == 2
            assert h.c.data["last_session_start"] == second
            assert h.c.data["last_session_id"] == SECOND_ID
            assert h.c.data["last_session_display_face"] == "special_6"
    asyncio.run(exercise())


@pytest.mark.parametrize("clock_offset", (-3600, -2400))
def test_brush_clock_reset_does_not_hide_a_distinct_session_id(clock_offset):
    async def exercise():
        async with _brush() as h:
            first = await _complete(h)
            await _recover(h, _record(first, 139, FIRST_ID))
            await h._advance(2400 - (h.clock.utcnow() - first).total_seconds())
            second = await _complete(h, face=6)
            raw = bytearray(_record(second, 139, SECOND_ID))
            raw[:4] = (_native_time(second) + clock_offset).to_bytes(4, "little")
            # -2400 reuses the first timestamp with a different session ID;
            # -3600 gives the new session an older native timestamp.
            await _recover(h, bytes(raw), clock_offset=clock_offset)
            assert h.c.data["sessions_today"] == 2
            assert h.c.data["last_session_start"] == second
            assert h.c.data["last_session_id"] == SECOND_ID
            assert h.c.data["last_session_display_face"] == "special_6"
    asyncio.run(exercise())


@pytest.mark.parametrize("clock_case", ("valid", "zero", "before_start", "before_end", "missing_then_valid"))
@pytest.mark.parametrize("record_first", (False, True))
def test_charger_forwarded_records_preserve_session_identity(clock_case, record_first):
    async def exercise():
        async with _brush() as h:
            _, second = await _two_sessions(h)
            h.c.charger.address = "11:22:33:44:55:66"
            await h.c._async_apply_charger_passthrough("FF05", b"\x11")
            raw = _record(second, 139)

            def clock():
                if clock_case == "valid" or clock_case == "missing_then_valid":
                    return _native_time(h.clock.utcnow()).to_bytes(4, "little")
                if clock_case == "zero":
                    return bytes(4)
                timestamp = int.from_bytes(raw[:4], "little")
                seconds = timestamp - 1 if clock_case == "before_start" else timestamp + 138
                return seconds.to_bytes(4, "little")

            if clock_case == "missing_then_valid":
                packets = (("FF29", raw), ("FF22", b"\x01")) if record_first else (
                    ("FF22", b"\x01"), ("FF29", raw),
                )
                for short_uuid, payload in packets:
                    await h.c._async_apply_charger_passthrough(short_uuid, payload)
                assert h.c.data["sessions_today"] == 2
                assert h.c.data["last_session_start"] == second
                await h._advance(2)
                await h.c._async_apply_charger_passthrough("FF22", clock())
            elif record_first:
                await h.c._async_apply_charger_passthrough("FF29", raw)
                await h._advance(2)
                await h.c._async_apply_charger_passthrough("FF22", clock())
            else:
                await h.c._async_apply_charger_passthrough("FF22", clock())
                await h._advance(2)
                await h.c._async_apply_charger_passthrough("FF29", raw)
            assert h.c.data["sessions_today"] == 2
            assert h.c.data["last_session_start"] == second
            assert h.c.data["last_session_display_face"] == "special_6"
            assert h.c.data["battery"] == 17
            assert h.c.data["battery_source"] == const.DATA_SOURCE_CHARGER
            if clock_case in ("valid", "missing_then_valid"):
                assert h.c.data["last_session_id"] == SECOND_ID
                assert h.c.data["last_session_source"] == const.DATA_SOURCE_SESSION
    asyncio.run(exercise())


@pytest.mark.parametrize("blocked_uuid", (const.CHAR_SESSION_DATA, const.CHAR_RTC))
@pytest.mark.parametrize("stop_during_read", (False, True))
def test_predecessor_read_cannot_overwrite_new_running_or_stopped_session(
    blocked_uuid, stop_during_read,
):
    async def exercise():
        async with _brush() as h:
            _, second = await _two_sessions(h)
            await h._advance(60)
            release = asyncio.Event()
            entered = asyncio.Event()
            raw = _record(second, 139)

            async def read(uuid):
                if uuid == blocked_uuid:
                    entered.set()
                    await release.wait()
                if uuid == const.CHAR_STATUS_BLOB:
                    return h.status
                if uuid == const.CHAR_SESSION_DATA:
                    return raw
                if uuid == const.CHAR_RTC:
                    return _native_time(h.clock.utcnow()).to_bytes(4, "little")
                return None

            h.read_hook = read
            await h._feed(2, 139)
            assert entered.is_set(), "Recovery never reached the intended await"
            third = h.clock.utcnow()
            await h._feed(3, 0)
            await h._advance(1)
            await h._feed(3, 1)
            if stop_during_read:
                await h._advance(1)
                await h._feed(9, 2, face=4)
            release.set()
            await h._ready()
            assert h.c.data["sessions_today"] == 2
            assert h.c.data["last_session_start"] == second
            if not stop_during_read:
                await h._advance(138)
                await h._feed(9, 139, face=4)
            await h._advance(20)
            assert h.c.data["sessions_today"] == 3
            assert h.c.data["last_session_start"] == third
            assert h.c.data["last_session_duration"] == (2 if stop_during_read else 139)
            assert h.c.data["last_session_display_face"] == "special_4"
            assert h.c.data["last_session_id"] != SECOND_ID
    asyncio.run(exercise())


@pytest.mark.parametrize("bad_record", (b"", b"\x01", bytes(21)))
def test_malformed_record_does_not_modify_completed_sessions(bad_record):
    async def exercise():
        async with _brush() as h:
            _, second = await _two_sessions(h)
            await _recover(h, bad_record)
            assert h.c.data["sessions_today"] == 2
            assert h.c.data["last_session_start"] == second
            assert h.c.data["last_session_duration"] == 139
            assert h.c.data["last_session_display_face"] == "special_6"
            assert h.c.data["battery"] == 23
    asyncio.run(exercise())


def test_timestamp_only_upgrade_remembers_the_legacy_record_before_a_new_one():
    async def exercise():
        async with _brush() as h:
            first = await _complete(h)
            first_record = _record(first, 139, FIRST_ID)
            await _recover(h, first_record)
            # Upgrade an installation whose store predates identity history.
            h.c._store.async_load = AsyncMock(return_value={
                "last_session_ts": _native_time(first),
            })
            await _reload(h)
            await _recover(h, first_record)
            assert h.c.data["sessions_today"] == 1
            await h._advance(2400 - (h.clock.utcnow() - first).total_seconds())
            second = await _complete(h, face=6)
            await _recover(h, _record(second, 139))
            h.clock.now += const.PERIODIC_SYNC_INTERVAL_SECONDS
            await h._advance(0)
            await _recover(h, first_record)
            assert h.c.data["sessions_today"] == 2
            assert h.c.data["last_session_start"] == second
            assert h.c.data["last_session_id"] == SECOND_ID
            assert h.c.data["last_session_display_face"] == "special_6"
    asyncio.run(exercise())


@pytest.mark.parametrize("phase", ("load", "save"))
@pytest.mark.parametrize("deferred", (False, True))
def test_storage_await_cannot_acknowledge_a_newer_charger_session(phase, deferred):
    async def exercise():
        async with _brush() as h:
            _, second = await _two_sessions(h)
            h.c.charger.address = "11:22:33:44:55:66"
            raw = _record(second, 139)
            rtc = _native_time(h.clock.utcnow()).to_bytes(4, "little")
            entered = asyncio.Event()
            release = asyncio.Event()
            original = getattr(h.c._store, f"async_{phase}")

            async def blocked(*args):
                entered.set()
                await release.wait()
                return await original(*args)

            setattr(h.c._store, f"async_{phase}", AsyncMock(side_effect=blocked))
            if deferred:
                apply = h.c._async_apply_deferred_charger_session_record(
                    raw, rtc, h.clock.utcnow(), h.clock.utcnow(),
                )
            else:
                await h.c._async_apply_charger_passthrough("FF29", raw)
                apply = h.c._async_apply_charger_passthrough("FF22", rtc)
            task = asyncio.create_task(apply)
            try:
                await h._ready()
                assert entered.is_set(), "The intended storage await was not reached"
                third = await _complete(h, face=4)
                generation = h.c._session_generation
                assert h.c._session_pending_sync
                release.set()
                await task
                assert h.c._session_pending_sync
                assert h.c._processed_session_generation < generation
                assert h.c.data["sessions_today"] == 3
                assert h.c.data["last_session_start"] == third
                assert h.c.data["last_session_display_face"] == "special_4"
                assert h.c.data["last_session_id"] is None
            finally:
                release.set()
                await task
    asyncio.run(exercise())


@pytest.mark.parametrize("stored", (
    None,
    [],
    {"last_session_ts": True, "session_records": "invalid"},
    {
        "last_session_ts": -1,
        "last_session_key": [1, True, 0],
        "session_records": [
            None, [], [True, 1, 0], [0, 1, 0], [1, -1, 0],
            [1, 8192, 0], [1, 1, 8], [2**32, 1, 0], [1, 1, "0"],
        ],
    },
))
def test_invalid_identity_storage_does_not_suppress_a_real_record(stored):
    async def exercise():
        async with _brush() as h:
            h.c.data["protocol_version"] = 7
            h.c._store.async_load = AsyncMock(return_value=stored)
            start = h.clock.utcnow() - timedelta(minutes=10)
            result = await h.c._async_apply_session_record(
                _record(start, 139),
                _native_time(h.clock.utcnow()).to_bytes(4, "little"),
            )
            assert result == "new"
            assert h.c.data["sessions_today"] == 1
            assert h.c.data["last_session_start"] == start
            saved = h.c._store.async_save.call_args.args[0]
            assert saved["session_records"] == [[_native_time(start), SECOND_ID, 0]]
    asyncio.run(exercise())


def test_record_history_is_bounded_and_keeps_the_latest_identities_on_reload():
    async def exercise():
        async with _brush() as h:
            h.c.data["protocol_version"] = 7
            identities = []
            total = const.SESSION_RECORD_HISTORY_SIZE + 1
            for index in range(total):
                start = h.clock.utcnow() - timedelta(minutes=5 * (total - index))
                session_id = FIRST_ID + index
                identities.append([_native_time(start), session_id, 0])
                assert await h.c._async_apply_session_record(
                    _record(start, 139, session_id),
                    _native_time(h.clock.utcnow()).to_bytes(4, "little"),
                ) == "new"
            saved = h.c._store.async_save.call_args.args[0]
            assert saved["session_records"] == identities[-const.SESSION_RECORD_HISTORY_SIZE:]
            assert saved["last_session_key"] == identities[-1]
            count = h.c.data["sessions_today"]
            await _reload(h)
            h.c.data["protocol_version"] = 7
            start = fixtures.WALL_START - timedelta(minutes=5)
            assert await h.c._async_apply_session_record(
                _record(start, 139, FIRST_ID + const.SESSION_RECORD_HISTORY_SIZE),
                None,
            ) == "duplicate"
            assert h.c.data["sessions_today"] == count
            assert h.c.data["last_session_start"] == start
    asyncio.run(exercise())


def test_simultaneous_first_record_reads_count_once():
    async def exercise():
        async with _brush() as h:
            h.c.data["protocol_version"] = 7
            entered = asyncio.Event()
            release = asyncio.Event()

            async def load():
                entered.set()
                await release.wait()
                return {}

            h.c._store.async_load = AsyncMock(side_effect=load)
            raw = _record(h.clock.utcnow() - timedelta(minutes=10), 139)
            rtc = _native_time(h.clock.utcnow()).to_bytes(4, "little")
            first = asyncio.create_task(h.c._async_apply_session_record(raw, rtc))
            second = asyncio.create_task(h.c._async_apply_session_record(raw, rtc))
            try:
                await h._ready()
                assert entered.is_set()
                release.set()
                results = await asyncio.gather(first, second)
                assert results.count("new") == 1
                assert h.c.data["sessions_today"] == 1
                h.c._store.async_load.assert_awaited_once()
                h.c._store.async_save.assert_awaited_once()
            finally:
                release.set()
                await asyncio.gather(first, second)
    asyncio.run(exercise())
