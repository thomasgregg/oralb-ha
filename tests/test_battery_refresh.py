"""Issue-39 contracts exercised without shortening production session delays.

Bluetooth callbacks come through HA's manager, including replay and unchanged-payload
filtering. Only transport, storage, and the coordinator's clock are replaced;
the session tracker, sync scheduler, read sequence, and parsers remain real.
"""

from __future__ import annotations

import asyncio
import importlib
import pathlib
import sys
import types
import unittest
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from typing import Any, Callable
from unittest.mock import AsyncMock, MagicMock, patch

from bleak.backends.device import BLEDevice
from bleak.backends.scanner import AdvertisementData
from habluetooth import BluetoothServiceInfoBleak
from homeassistant.components.bluetooth.manager import HomeAssistantBluetoothManager


COMPONENT_PATH = pathlib.Path(__file__).parents[1] / "custom_components" / "oralb_live"
_PACKAGE = types.ModuleType("oralb_live")
_PACKAGE.__path__ = [str(COMPONENT_PATH)]
sys.modules.setdefault("oralb_live", _PACKAGE)
const = importlib.import_module("oralb_live.const")
coordinator = importlib.import_module("oralb_live.coordinator")
sensor = importlib.import_module("oralb_live.sensor")

ADDRESS = "AA:BB:CC:DD:EE:FF"
WALL_START = datetime(2026, 10, 1, 12, tzinfo=timezone.utc)


class _ModuleProxy:
    """Override the coordinator's clock without altering asyncio's real loop."""

    def __init__(self, original: Any, **overrides: Any) -> None:
        self.original = original
        self.overrides = overrides

    def __getattr__(self, name: str) -> Any:
        if name in self.overrides:
            return self.overrides[name]
        return getattr(self.original, name)


class _Timer:
    def __init__(self, deadline: float, callback: Callable[[], None]) -> None:
        self.deadline = deadline
        self.callback = callback
        self._cancelled = False

    def cancel(self) -> None:
        self._cancelled = True

    def cancelled(self) -> bool:
        return self._cancelled

    def when(self) -> float:
        return self.deadline


class _Clock:
    """Run actual background tasks against manually advanced deadlines."""

    def __init__(self) -> None:
        self.now = 1000.0
        self.timers: list[_Timer] = []

    def utcnow(self) -> datetime:
        return WALL_START + timedelta(seconds=self.now - 1000.0)

    def call_at(self, deadline: float, callback: Callable, *args: Any) -> _Timer:
        timer = _Timer(deadline, lambda: callback(*args))
        self.timers.append(timer)
        return timer

    def call_later(self, delay: float, callback: Callable, *args: Any) -> _Timer:
        return self.call_at(self.now + delay, callback, *args)

    async def sleep(self, delay: float) -> None:
        if delay <= 0:
            await asyncio.sleep(0)
            return
        future = asyncio.get_running_loop().create_future()
        timer = self.call_later(delay, lambda: future.set_result(None))
        try:
            await future
        finally:
            timer.cancel()

    @asynccontextmanager
    async def timeout(self, delay: float | None):
        if delay is None:
            yield
            return
        task = asyncio.current_task()
        expired = False
        def expire():
            nonlocal expired
            expired = True
            task.cancel()
        timer = self.call_later(delay, expire)
        try:
            yield
        except asyncio.CancelledError:
            if expired:
                task.uncancel()
                raise TimeoutError from None
            raise
        finally:
            timer.cancel()

    async def wait_for(self, awaitable, timeout: float | None):
        async with self.timeout(timeout):
            return await awaitable


def _service_info(
    state: int,
    seconds: int,
    observed_at: float,
    *,
    face: int = 0,
    protocol: int = 7,
    connectable: bool = True,
) -> BluetoothServiceInfoBleak:
    # The wire timer is minutes/seconds, not a big-endian integer. Use a
    # multi-minute session so a mistaken fixture encoding cannot go unnoticed.
    payload = bytes(
        [protocol, 0x31, 36, state, 0x72, seconds // 60, seconds % 60,
         0, 1 | (face << 3), 0, 4]
    )
    manufacturer_data = {const.ORALB_MANUFACTURER_ID: payload}
    device = BLEDevice(ADDRESS, "brush", {}, -60)
    advertisement = AdvertisementData(
        "brush", manufacturer_data, {}, [], None, -60, ()
    )
    return BluetoothServiceInfoBleak(
        "brush", ADDRESS, -60, manufacturer_data, {}, [], "test_proxy",
        device, advertisement, connectable, observed_at,
    )


def _record(*, timestamp: int = 1000, duration: int = 120, battery: int = 94) -> bytes:
    payload = bytearray.fromhex("26e4ff3161017800800064000a001321280201045e")
    payload[:4] = timestamp.to_bytes(4, "little")
    payload[8:10] = duration.to_bytes(2, "little")
    payload[20] = battery
    return bytes(payload)


class BatteryRefreshTests(unittest.IsolatedAsyncioTestCase):
    """Protect externally visible battery and session behavior together."""

    async def asyncSetUp(self) -> None:
        self.clock = _Clock()
        self.tasks: list[asyncio.Task] = []
        self.publications: list[dict[str, Any]] = []
        self.connections: list[float] = []
        self.reads: list[tuple[float, str]] = []
        self.callback_times: list[float] = []
        self.status: bytes | None = b"\x17"  # 23%, replacing a restored 99%.
        self.responses: dict[str, bytes | Exception | None] = {}
        self.read_hook: Callable | None = None
        self.connection_error: Exception | None = None
        self.hass = MagicMock()
        self.hass.loop = _ModuleProxy(
            asyncio.get_running_loop(), time=lambda: self.clock.now,
            call_at=self.clock.call_at, call_later=self.clock.call_later,
        )
        self.hass.async_create_task.side_effect = self._create_task
        self.hass.async_create_background_task.side_effect = self._create_task
        matcher = MagicMock()
        matcher.match_domains.return_value = []
        self.manager = HomeAssistantBluetoothManager(
            self.hass, matcher, MagicMock(), MagicMock(), MagicMock()
        )
        self.patches = [
            patch.object(coordinator, "time", _ModuleProxy(
                coordinator.time, monotonic=lambda: self.clock.now)),
            patch.object(coordinator, "asyncio", _ModuleProxy(
                asyncio, sleep=self.clock.sleep, timeout=self.clock.timeout,
                wait_for=self.clock.wait_for)),
            patch.object(coordinator, "dt_util", _ModuleProxy(
                coordinator.dt_util, utcnow=self.clock.utcnow,
                now=lambda: coordinator.dt_util.as_local(self.clock.utcnow()))),
            patch.object(coordinator, "async_dispatcher_send", side_effect=self._publish),
            patch.object(coordinator, "async_track_time_change", return_value=lambda: None),
            patch.object(coordinator.bluetooth, "async_register_callback",
                         side_effect=self._register_callback),
            patch.object(coordinator.bluetooth, "async_track_unavailable",
                         side_effect=lambda hass, cb, address, connectable:
                         self.manager.async_track_unavailable(cb, address, connectable)),
            patch.object(coordinator.bluetooth, "async_last_service_info",
                         side_effect=lambda hass, address, connectable=False:
                         self.manager.async_last_service_info(address, connectable)),
            patch.object(coordinator.bluetooth, "async_ble_device_from_address",
                         side_effect=lambda hass, address, connectable=True:
                         self.manager.async_ble_device_from_address(address, connectable)),
        ]
        for item in self.patches:
            item.start()
        self.c = coordinator.OralBLiveCoordinator(
            self.hass, ADDRESS, "test", const.CONNECTION_MODE_CHARGER
        )
        self.c._store.async_load = AsyncMock(return_value={})
        self.c._store.async_save = AsyncMock()
        self.c.data.update(
            battery=99, battery_source="restored",
            battery_updated_at=WALL_START - timedelta(days=9),
        )
        self.client = MagicMock()
        self.client.is_connected = False
        self.client.read_gatt_char = AsyncMock(side_effect=self._read)
        self.client.start_notify = AsyncMock()
        self.client.disconnect = AsyncMock(side_effect=self._disconnect)
        self.c._async_establish_brush_connection = AsyncMock(side_effect=self._connect)

    async def asyncTearDown(self) -> None:
        try:
            await self.c.async_stop()
        finally:
            for task in self.tasks:
                task.cancel()
            await asyncio.gather(*self.tasks, return_exceptions=True)
            for item in reversed(self.patches):
                item.stop()

    def _create_task(self, coro, name=None, **kwargs) -> asyncio.Task:
        task = asyncio.create_task(coro, name=name)
        self.tasks.append(task)
        return task

    def _publish(self, hass, signal, data) -> None:
        if signal == f"{const.SIGNAL_UPDATE}_{ADDRESS}":
            self.publications.append(dict(data))

    def _register_callback(self, hass, callback, matcher, mode):
        def receive(info, change):
            if matcher.get("address") == ADDRESS:
                self.callback_times.append(info.time)
            callback(info, change)
        return self.manager.async_register_callback(receive, matcher)

    async def _connect(self, *args, **kwargs):
        self.connections.append(self.clock.now)
        if self.connection_error is not None:
            raise self.connection_error
        self.client.is_connected = True
        return self.client

    async def _disconnect(self) -> None:
        self.client.is_connected = False

    async def _read(self, uuid: str):
        self.reads.append((self.clock.now, uuid))
        if self.read_hook is not None:
            return await self.read_hook(uuid)
        if uuid in self.responses:
            result = self.responses[uuid]
            if isinstance(result, Exception):
                raise result
            return result
        if uuid == const.CHAR_STATUS_BLOB:
            return self.status
        if uuid == const.CHAR_STATE:
            info = self.manager.async_last_service_info(ADDRESS, False)
            return bytes((info.manufacturer_data[220][3],)) if info else None
        return None

    async def _ready(self) -> None:
        # Drain runnable coroutines without advancing either production delay.
        for _ in range(16):
            await asyncio.sleep(0)
        for task in self.tasks:
            if task.done() and not task.cancelled() and task.exception() is not None:
                raise task.exception()

    async def _advance(self, seconds: float) -> None:
        await self._ready()
        target = self.clock.now + seconds
        for _ in range(1000):
            pending = [t for t in self.clock.timers if not t.cancelled()
                       and t.deadline <= target]
            if not pending:
                self.clock.now = target
                await self._ready()
                return
            timer = min(pending, key=lambda t: t.deadline)
            self.clock.now = max(self.clock.now, timer.deadline)
            timer.cancel()
            timer.callback()
            await self._ready()
        self.fail("Virtual deadline loop did not converge")

    async def _feed(self, state: int, seconds: int = 0, **kwargs) -> None:
        self.manager.scanner_adv_received(
            _service_info(state, seconds, self.clock.now, **kwargs)
        )
        await self._ready()

    async def _stop_session(
        self, state: int = 9, *, face: int = 3, protocol: int = 7
    ) -> None:
        self.c.async_start()
        await self._feed(3, protocol=protocol)
        await self._advance(60)
        await self._feed(3, 60, protocol=protocol)
        await self._advance(60)
        await self._feed(state, 120, face=face, protocol=protocol)

    def _assert_published_battery(self, value: int) -> None:
        self.assertTrue(any(p.get("battery") == value for p in self.publications),
                        f"Battery {value}% was not published")

    async def test_summary_updates_battery_before_pause_grace_expires(self) -> None:
        await self._stop_session(9)
        await self._advance(10)
        self._assert_published_battery(23)
        self.assertIsNone(self.c.data["last_session_start"])
        self.assertFalse(self.client.is_connected)

    async def test_post_brushing_summary_also_updates_battery_promptly(self) -> None:
        await self._stop_session(10)
        await self._advance(10)
        self._assert_published_battery(23)
        self.assertFalse(self.client.is_connected)

    async def test_complete_off_dock_sequence_keeps_one_session_and_fresh_battery(self) -> None:
        await self._stop_session(9)
        await self._advance(31)
        await self._feed(8, 120)
        await self._advance(10)
        await self._feed(115, 120)
        await self._advance(60)
        self.assertEqual(self.c.data["battery"], 23)
        self.assertEqual(self.c.data["sessions_today"], 1)
        self.assertEqual(self.c.data["last_session_duration"], 120)
        self.assertEqual(self.c.data["last_session_display_face"], "special_3")

    async def test_idle_still_refreshes_battery_and_disconnects(self) -> None:
        self.c.async_start()
        await self._feed(2)
        self._assert_published_battery(23)
        self.assertFalse(self.client.is_connected)
        self.assertIsNone(self.c.data["last_session_start"])

    async def test_charging_still_refreshes_battery(self) -> None:
        self.c.async_start()
        await self._feed(4)
        self._assert_published_battery(23)
        self.assertIsNone(self.c.data["sessions_today"])

    async def test_running_does_not_trigger_a_maintenance_connection(self) -> None:
        self.c.async_start()
        await self._feed(3)
        await self._advance(10)
        await self._feed(3, 10)
        self.assertEqual(self.connections, [])

    async def test_selection_menu_with_advancing_timer_is_not_a_quiet_window(self) -> None:
        self.c.async_start()
        await self._feed(8)
        await self._advance(10)
        await self._feed(8, 10)
        self.assertEqual(self.connections, [])
        self.assertTrue(self.c._session_confirmed)

    async def test_menu_button_wake_does_not_invent_a_session_or_read_battery(self) -> None:
        self.c.async_start()
        await self._feed(8)
        await self._advance(10)
        await self._feed(115)
        self.assertEqual(self.connections, [])
        self.assertIsNone(self.c.data["last_session_start"])

    async def test_known_charger_prevents_direct_maintenance(self) -> None:
        self.c.charger.address = "11:22:33:44:55:66"
        self.c.async_start()
        await self._feed(4)
        await self._advance(60)
        self.assertEqual(self.connections, [])

    async def test_nonconnectable_scanner_cannot_refresh_battery(self) -> None:
        self.c.async_start()
        await self._feed(4, connectable=False)
        await self._advance(60)
        self.assertEqual(self.connections, [])
        self.assertEqual(self.c.data["battery"], 99)

    async def test_recent_history_without_available_device_does_not_spin(self) -> None:
        with patch.object(coordinator.bluetooth, "async_ble_device_from_address",
                          return_value=None) as device_lookup:
            self.c.async_start()
            await self._feed(4)
            self.assertEqual(device_lookup.call_count, 1)
            await self._advance(14)
            self.assertEqual(device_lookup.call_count, 1)
            await self._advance(1)
            self.assertEqual(device_lookup.call_count, 2)
            self.assertEqual(self.connections, [])

    async def test_success_does_not_cause_repeated_quiet_connections(self) -> None:
        self.c.async_start()
        await self._feed(4)
        await self._advance(61)
        await self._feed(4)
        await self._advance(61)
        await self._feed(2)
        self.assertEqual(len(self.connections), 1)

    async def test_cooldown_retries_using_fresh_unchanged_history(self) -> None:
        self.c._last_sync_attempt = self.clock.now
        self.c.async_start()
        await self._feed(4)
        await self._advance(59)
        await self._feed(4)  # HA updates history but suppresses this callback.
        self.assertEqual(self.callback_times, [1000.0])
        self.assertEqual(self.manager.async_last_service_info(ADDRESS, True).time, 1059)
        self.assertEqual(self.connections, [])
        await self._advance(2)
        self._assert_published_battery(23)
        self.assertEqual(len(self.connections), 1)

    async def test_periodic_refresh_uses_fresh_history_when_payload_does_not_change(self) -> None:
        self.c.async_start()
        await self._feed(4)
        await self._advance(const.PERIODIC_SYNC_INTERVAL_SECONDS - 1)
        self.status = b"\x16"
        await self._feed(4)
        self.assertEqual(len(self.callback_times), 1)
        await self._advance(2)
        self._assert_published_battery(22)
        self.assertEqual(len(self.connections), 2)

    async def test_expired_summary_does_not_authorize_a_cooldown_retry(self) -> None:
        self.c._last_sync_attempt = self.clock.now
        self.c.async_start()
        await self._feed(9, 120)
        # No more packets: cached summary is not evidence the handle is awake.
        await self._advance(61)
        self.assertEqual(self.connections, [])

    async def test_cooldown_does_not_connect_after_brush_goes_to_sleep(self) -> None:
        self.c._last_sync_attempt = self.clock.now
        self.c.async_start()
        await self._feed(4)
        await self._advance(10)
        await self._feed(115)
        await self._advance(60)
        self.assertEqual(self.connections, [])

    async def test_charger_discovery_during_cooldown_cancels_direct_opportunity(self) -> None:
        self.c._last_sync_attempt = self.clock.now
        self.c.async_start()
        await self._feed(4)
        await self._advance(30)
        self.c.charger.address = "11:22:33:44:55:66"
        await self._advance(31)
        self.assertEqual(self.connections, [])

    async def test_failed_battery_read_retries_without_a_changed_advertisement(self) -> None:
        self.status = None
        self.c.async_start()
        await self._feed(4)
        self.assertEqual(self.c.data["battery"], 99)
        self.status = b"\x17"
        await self._advance(59)
        await self._feed(4)
        await self._advance(2)
        self._assert_published_battery(23)
        self.assertEqual(len(self.connections), 2)

    async def test_phone_contention_preserves_battery_and_releases_retry_on_sleep(self) -> None:
        self.connection_error = coordinator.BleakError("single slot occupied")
        self.c.async_start()
        await self._feed(4)
        await self._advance(10)
        await self._feed(115)
        await self._advance(120)
        self.assertEqual(self.c.data["battery"], 99)
        self.assertEqual(len(self.connections), 1)
        self.assertFalse(self.client.is_connected)

    async def test_cached_quiet_replay_does_not_authorize_a_connection(self) -> None:
        await self._feed(4)
        await self._advance(300)
        self.c.async_start()
        await self._ready()
        self.assertEqual(self.connections, [])
        await self._feed(2)
        self._assert_published_battery(23)

    async def test_cached_running_replay_does_not_open_a_session(self) -> None:
        await self._feed(3, 120)
        await self._advance(300)
        self.c.async_start()
        await self._ready()
        self.assertFalse(self.c._session_active)
        self.assertEqual(self.connections, [])

    async def test_recent_cached_quiet_packet_needs_a_new_observation(self) -> None:
        await self._feed(4)
        self.c.async_start()
        await self._advance(15)
        self.assertEqual(self.connections, [])

    async def test_fresh_unchanged_packet_after_reload_refreshes_battery(self) -> None:
        await self._feed(4)
        self.c.async_start()
        await self._advance(10)
        await self._feed(4)
        # Only the registration replay reached the coordinator callback.
        self.assertEqual(self.callback_times, [1000.0])
        await self._advance(5)
        self._assert_published_battery(23)
        self.assertEqual(len(self.connections), 1)

    async def test_sleep_while_waiting_for_connection_lock_prevents_connection(self) -> None:
        await self.c._connect_lock.acquire()
        try:
            self.c.async_start()
            await self._feed(4)
            await self._feed(115)
        finally:
            self.c._connect_lock.release()
        await self._ready()
        self.assertEqual(self.connections, [])

    async def test_charger_found_while_waiting_for_lock_prevents_connection(self) -> None:
        await self.c._connect_lock.acquire()
        try:
            self.c.async_start()
            await self._feed(4)
            self.c.charger.address = "11:22:33:44:55:66"
        finally:
            self.c._connect_lock.release()
        await self._ready()
        self.assertEqual(self.connections, [])

    async def test_packet_expiring_while_waiting_for_lock_prevents_connection(self) -> None:
        await self.c._connect_lock.acquire()
        try:
            self.c.async_start()
            await self._feed(4)
            await self._advance(16)
        finally:
            self.c._connect_lock.release()
        await self._ready()
        self.assertEqual(self.connections, [])

    async def test_sleep_during_status_read_stops_optional_reads(self) -> None:
        release = asyncio.Event()
        async def read(uuid):
            if uuid == const.CHAR_STATUS_BLOB:
                await release.wait()
                return b"\x17"
            return None
        self.read_hook = read
        self.c.async_start()
        await self._feed(4)
        await self._feed(115)
        release.set()
        await self._ready()
        self._assert_published_battery(23)
        self.assertEqual([uuid for _, uuid in self.reads], [const.CHAR_STATUS_BLOB])
        self.assertFalse(self.client.is_connected)

    async def test_nonconnectable_sleep_overrides_older_connectable_quiet_packet(self) -> None:
        self.c._last_sync_attempt = self.clock.now
        self.c.async_start()
        await self._feed(4)
        await self._advance(59)
        await self._feed(4)
        await self._feed(115, connectable=False)
        await self._advance(2)
        self.assertEqual(self.connections, [])

    async def test_new_stop_refreshes_even_after_recent_success(self) -> None:
        self.c.async_start()
        await self._feed(4)
        self.status = b"\x16"  # 22% after brushing, less than six hours later.
        await self._feed(3)
        await self._advance(120)
        await self._feed(9, 120)
        await self._advance(10)
        self._assert_published_battery(22)

    async def test_resolved_record_does_not_drop_failed_post_session_battery(self) -> None:
        self.c.async_start()
        await self._feed(4, protocol=6)  # A valid battery read less than six hours ago.
        self.responses[const.CHAR_SESSION_DATA] = bytes(20)  # Known unsupported layout.
        self.status = None
        await self._feed(3, protocol=6)
        await self._advance(120)
        await self._feed(2, 120, protocol=6)
        await self._advance(31)
        self.assertEqual(self.c.data["battery"], 23)
        self.status = b"\x16"
        await self._advance(60)
        await self._feed(4, protocol=6)
        self._assert_published_battery(22)

    async def test_summary_only_refresh_does_not_invent_a_local_session(self) -> None:
        self.c.async_start()
        await self._feed(9, 120, face=3)
        await self._advance(10)
        self._assert_published_battery(23)
        self.assertIsNone(self.c.data["last_session_start"])
        self.assertIsNone(self.c.data["sessions_today"])

    async def test_successful_battery_does_not_resolve_a_missing_session_record(self) -> None:
        await self._stop_session(2)
        await self._advance(31)
        self._assert_published_battery(23)
        self.assertEqual(self.c.data["sessions_today"], 1)
        self.assertEqual(self.c.data["last_session_duration"], 120)
        self.assertEqual(self.c.data["last_session_source"], const.DATA_SOURCE_ADVERTISEMENT)
        self.assertGreater(self.c._session_generation, self.c._processed_session_generation)

    async def test_record_retries_honour_cooldown_and_then_back_off(self) -> None:
        await self._stop_session(2)
        for _ in range(5):
            await self._advance(59)
            await self._feed(2, 120, face=3)
            await self._advance(1)
        # One prompt battery connection, four immediate record attempts and one
        # deferred attempt; the following record retry waits five minutes.
        self.assertEqual(len(self.connections), 6)
        self.assertTrue(all(b - a >= const.SYNC_MIN_INTERVAL_SECONDS
                            for a, b in zip(self.connections, self.connections[1:])))
        await self._advance(299)
        await self._feed(2, 120, face=3)
        self.assertEqual(len(self.connections), 6)
        await self._advance(1)
        self.assertEqual(len(self.connections), 7)
        self.assertEqual(self.c.data["sessions_today"], 1)

    async def test_resumed_session_record_waits_for_the_latest_stop(self) -> None:
        await self._stop_session(9)
        await self._advance(55)
        await self._feed(8, 120)
        await self._feed(3, 120)
        await self._advance(5)
        await self._feed(9, 125, face=6)
        self.assertEqual([uuid for _, uuid in self.reads].count(const.CHAR_SESSION_DATA), 0)
        await self._advance(29)
        await self._feed(9, 125, face=6)
        self.assertEqual([uuid for _, uuid in self.reads].count(const.CHAR_SESSION_DATA), 0)
        await self._advance(30)
        await self._feed(9, 125, face=6)
        await self._advance(1)
        self.assertEqual([uuid for _, uuid in self.reads].count(const.CHAR_SESSION_DATA), 1)
        self.assertEqual(self.c.data["sessions_today"], 1)
        self.assertEqual(self.c.data["last_session_duration"], 125)

    async def test_sampled_maintenance_state_does_not_open_a_session(self) -> None:
        self.responses[const.CHAR_STATE] = b"\x03"
        self.c.async_start()
        await self._feed(4)
        self._assert_published_battery(23)
        self.assertFalse(self.c._session_active)
        self.assertEqual(self.c.data["state_raw"], 4)

    async def test_protocol_6_summary_refresh_preserves_the_passive_session(self) -> None:
        self.status = bytes.fromhex("3b 00 00 00")
        self.responses[const.CHAR_SESSION_DATA] = bytes(20)
        await self._stop_session(9, protocol=6)
        await self._advance(10)
        self._assert_published_battery(59)
        await self._advance(10)
        self.assertEqual(self.c.data["sessions_today"], 1)
        self.assertEqual(self.c.data["last_session_duration"], 120)
        self.assertEqual(self.c.data["last_session_source"], const.DATA_SOURCE_ADVERTISEMENT)

    async def test_pause_resume_preserves_start_count_duration_and_final_face(self) -> None:
        await self._stop_session(9)
        original_start = self.c._session_start
        await self._advance(5)
        await self._feed(3, 120)
        await self._advance(5)
        await self._feed(3, 125)
        await self._feed(9, 125, face=6)
        await self._advance(19)
        self.assertIsNone(self.c.data["last_session_start"])
        await self._advance(1)
        self.assertEqual(self.c.data["last_session_start"], original_start)
        self.assertEqual(self.c.data["sessions_today"], 1)
        self.assertEqual(self.c.data["last_session_duration"], 125)
        self.assertEqual(self.c.data["last_session_display_face"], "special_6")

    async def test_two_sessions_with_timer_reset_still_count_twice(self) -> None:
        await self._stop_session(9)
        await self._advance(21)
        await self._feed(8)
        await self._advance(1)
        await self._feed(3, 1)
        await self._advance(4)
        await self._feed(9, 5, face=6)
        await self._advance(20)
        self.assertEqual(self.c.data["sessions_today"], 2)
        self.assertEqual(self.c.data["last_session_duration"], 5)
        self.assertEqual(self.c.data["last_session_display_face"], "special_6")

    async def test_direct_mode_keeps_its_notification_connection(self) -> None:
        self.c.mode = const.CONNECTION_MODE_LIVE
        self.c.charger = None
        self.c.async_start()
        await self._feed(4)
        self._assert_published_battery(23)
        self.assertTrue(self.client.is_connected)
        self.assertTrue(self.c.data["live"])
        self.client.start_notify.assert_any_await(const.CHAR_STATE, self.c._on_notify)

    async def test_unload_cancels_waits_and_preserves_a_stopped_session(self) -> None:
        await self._stop_session(9)
        await self._advance(5)
        await self.c.async_stop()
        await self._ready()
        connection_count = len(self.connections)
        await self._advance(120)
        await self._feed(4)
        self.assertEqual(len(self.connections), connection_count)
        self.assertEqual(self.c.data["sessions_today"], 1)
        self.assertEqual(self.c.data["last_session_duration"], 120)

    async def test_cancellation_during_read_disconnects_client(self) -> None:
        blocked = asyncio.Event()
        async def read(uuid):
            await blocked.wait()
        self.read_hook = read
        self.c.async_start()
        await self._feed(4)
        self.assertTrue(self.client.is_connected)
        await self.c.async_stop()
        await self._ready()
        self.assertFalse(self.client.is_connected)
        self.client.disconnect.assert_awaited_once()

    async def test_stalled_read_cannot_hold_brush_slot_indefinitely(self) -> None:
        blocked = asyncio.Event()
        async def read(uuid):
            await blocked.wait()
        self.read_hook = read
        self.c.async_start()
        await self._feed(4)
        self.assertTrue(self.client.is_connected)
        # A generous software upper bound; this is not a hardware latency claim.
        await self._advance(const.SYNC_MIN_INTERVAL_SECONDS + 1)
        self.assertFalse(self.client.is_connected)
        self.assertEqual(self.c.data["battery"], 99)

    async def test_timeout_during_service_cache_recovery_disconnects_owned_client(self) -> None:
        blocked = asyncio.Event()
        self.client.services.get_characteristic.return_value = None
        async def clear_cache():
            await blocked.wait()
        self.client.clear_cache = AsyncMock(side_effect=clear_cache)
        # Exercise the actual setup helper, not the transport shortcut.
        del self.c._async_establish_brush_connection
        async def establish(*args, **kwargs):
            self.client.is_connected = True
            return self.client
        with patch.object(coordinator, "establish_connection", side_effect=establish):
            self.c.async_start()
            await self._feed(4)
            self.assertTrue(self.client.is_connected)
            await self._advance(21)
            self.assertFalse(self.client.is_connected)
            self.client.disconnect.assert_awaited_once()

    async def test_whole_connection_deadline_bounds_many_slow_optional_reads(self) -> None:
        blocked = asyncio.Event()
        async def read(uuid):
            await blocked.wait()
        self.read_hook = read
        self.c.async_start()
        await self._feed(4)
        # Keep quiet history recent so the total deadline, rather than packet
        # expiration or one read's timeout, must end this sequence of reads.
        for _ in range(10):
            await self._advance(2)
            await self._feed(4)
        self.assertFalse(self.client.is_connected)
        self.assertEqual(len(self.connections), 1)
        self.client.disconnect.assert_awaited_once()
        self.assertEqual(self.c.data["battery"], 99)

    async def test_unload_during_service_cache_recovery_disconnects_owned_client(self) -> None:
        blocked = asyncio.Event()
        self.client.services.get_characteristic.return_value = None
        async def clear_cache():
            await blocked.wait()
        self.client.clear_cache = AsyncMock(side_effect=clear_cache)
        del self.c._async_establish_brush_connection
        async def establish(*args, **kwargs):
            self.client.is_connected = True
            return self.client
        with patch.object(coordinator, "establish_connection", side_effect=establish):
            self.c.async_start()
            await self._feed(4)
            self.assertTrue(self.client.is_connected)
            await self.c.async_stop()
            await self._ready()
            self.assertFalse(self.client.is_connected)
            self.client.disconnect.assert_awaited_once()

    async def test_unload_cancels_cooldown_retry_without_connecting(self) -> None:
        self.c._last_sync_attempt = self.clock.now
        self.c.async_start()
        await self._feed(4)
        await self.c.async_stop()
        await self._advance(61)
        await self._feed(2)
        self.assertEqual(self.connections, [])

    async def test_optional_slow_read_cannot_delay_battery_publication(self) -> None:
        blocked = asyncio.Event()
        async def read(uuid):
            if uuid == const.CHAR_STATUS_BLOB:
                return b"\x17"
            if uuid == const.CHAR_REFILL_REMAINDER:
                await blocked.wait()
            return None
        self.read_hook = read
        self.c.async_start()
        await self._feed(4)
        self._assert_published_battery(23)

    async def test_optional_unavailable_refill_cannot_hide_battery(self) -> None:
        self.responses[const.CHAR_REFILL_REMAINDER] = bytes.fromhex("00 0a 00 ff ff")
        self.c.async_start()
        await self._feed(4)
        self._assert_published_battery(23)
        self.assertFalse(self.client.is_connected)

    async def test_slow_record_read_cannot_precede_battery_publication(self) -> None:
        blocked = asyncio.Event()
        async def read(uuid):
            if uuid == const.CHAR_STATUS_BLOB:
                return b"\x17"
            if uuid == const.CHAR_SESSION_DATA:
                await blocked.wait()
            return None
        self.read_hook = read
        self.c.async_start()
        await self._feed(4)
        self._assert_published_battery(23)

    async def test_gatt_error_on_record_does_not_prevent_battery_read(self) -> None:
        self.responses[const.CHAR_SESSION_DATA] = coordinator.BleakError("unsupported")
        self.c.async_start()
        await self._feed(4)
        self._assert_published_battery(23)
        self.assertFalse(self.client.is_connected)

    async def test_real_zero_battery_is_published(self) -> None:
        self.status = b"\x00"
        self.c.async_start()
        await self._feed(4)
        self._assert_published_battery(0)
        self.assertEqual(self.c.data["battery_source"], const.DATA_SOURCE_DIRECT)

    async def test_unchanged_percentage_still_refreshes_timestamp_and_source(self) -> None:
        self.status = b"\x63"  # The fresh read is also 99%.
        self.c.async_start()
        await self._feed(4)
        self.assertEqual(self.c.data["battery"], 99)
        self.assertEqual(self.c.data["battery_updated_at"], self.clock.utcnow())
        self.assertEqual(self.c.data["battery_source"], const.DATA_SOURCE_DIRECT)

    async def test_empty_status_preserves_last_valid_value_and_timestamp(self) -> None:
        self.status = b""
        previous = self.c.data["battery_updated_at"]
        self.c.async_start()
        await self._feed(4)
        self.assertEqual(self.c.data["battery"], 99)
        self.assertEqual(self.c.data["battery_updated_at"], previous)

    async def test_invalid_record_cannot_replace_a_successful_battery_read(self) -> None:
        self.responses[const.CHAR_SESSION_DATA] = bytes(21)
        self.c.async_start()
        await self._feed(4)
        self._assert_published_battery(23)
        self.assertEqual(self.c.data["battery_source"], const.DATA_SOURCE_DIRECT)

    async def test_invalid_percentage_preserves_last_valid_value_and_timestamp(self) -> None:
        self.status = b"\xff"
        previous = self.c.data["battery_updated_at"]
        self.c.async_start()
        await self._feed(4)
        self.assertEqual(self.c.data["battery"], 99)
        self.assertEqual(self.c.data["battery_updated_at"], previous)

    async def test_protocol_6_battery_is_independent_of_unsupported_record_layout(self) -> None:
        self.status = bytes.fromhex("3b 00 00 00")
        self.responses[const.CHAR_SESSION_DATA] = bytes(20)
        self.c.async_start()
        await self._feed(4, protocol=6)
        self._assert_published_battery(59)
        self.assertIsNone(self.c.data["battery_time_remaining"])

    async def test_unknown_record_protocol_does_not_suppress_valid_battery(self) -> None:
        self.responses[const.CHAR_SESSION_DATA] = bytes(21)
        self.c.async_start()
        await self._feed(4, protocol=99)
        self._assert_published_battery(23)

    async def test_charger_battery_passthrough_publishes_without_direct_connection(self) -> None:
        self.c.charger.address = "11:22:33:44:55:66"
        await self.c._async_apply_charger_passthrough("FF05", b"\x17")
        self._assert_published_battery(23)
        self.assertEqual(self.c.data["battery_source"], const.DATA_SOURCE_CHARGER)
        self.assertEqual(self.connections, [])
        self.assertIsNone(self.c.data["last_session_start"])

    async def test_protocol_8_current_battery_diagnostics_remain_available(self) -> None:
        self.status = bytes.fromhex("17 10 0e a0 0f 7b 00 19")
        self.c.async_start()
        await self._feed(4, protocol=8)
        self._assert_published_battery(23)
        self.assertEqual(self.c.data["battery_time_remaining"], 3600)
        self.assertEqual(self.c.data["battery_voltage"], 4.0)
        self.assertEqual(self.c.data["battery_current"], 123)
        self.assertEqual(self.c.data["battery_temperature"], 25)

    async def test_duplicate_record_cannot_replace_fresh_direct_battery(self) -> None:
        self.c.data["protocol_version"] = 7
        self.c._last_synced_session_ts = 1000
        self.c._apply_battery_status(b"\x17", const.DATA_SOURCE_DIRECT)
        sampled_at = self.c.data["battery_updated_at"]
        await self.c._async_apply_session_record(
            _record(), (1600).to_bytes(4, "little"),
            rtc_sampled_at=self.clock.utcnow(),
        )
        self.assertEqual(self.c.data["battery"], 23)
        self.assertEqual(self.c.data["battery_source"], const.DATA_SOURCE_DIRECT)
        self.assertEqual(self.c.data["battery_updated_at"], sampled_at)

    async def test_full_sync_publishes_current_battery_instead_of_old_record(self) -> None:
        self.c._last_synced_session_ts = 1000
        self.responses[const.CHAR_SESSION_DATA] = _record()
        self.responses[const.CHAR_RTC] = (1600).to_bytes(4, "little")
        self.c.async_start()
        await self._feed(4)
        self._assert_published_battery(23)
        self.assertEqual(self.c.data["battery"], 23)

    async def test_previously_unseen_old_record_cannot_replace_current_battery(self) -> None:
        self.c.data["protocol_version"] = 7
        self.c._last_synced_session_ts = 500
        self.c._apply_battery_status(b"\x17", const.DATA_SOURCE_DIRECT)
        await self.c._async_apply_session_record(
            _record(), (1600).to_bytes(4, "little"),
            rtc_sampled_at=self.clock.utcnow(),
        )
        self.assertEqual(self.c.data["battery"], 23)
        self.assertEqual(self.c.data["battery_source"], const.DATA_SOURCE_DIRECT)

    async def test_duplicate_record_cannot_replace_fresh_charger_battery(self) -> None:
        self.c.data["protocol_version"] = 7
        self.c._last_synced_session_ts = 1000
        await self.c._async_apply_charger_passthrough("FF05", b"\x17")
        await self.c._async_apply_charger_passthrough("FF29", _record())
        await self.c._async_apply_charger_passthrough("FF22", (1600).to_bytes(4, "little"))
        self.assertEqual(self.c.data["battery"], 23)
        self.assertEqual(self.c.data["battery_source"], const.DATA_SOURCE_CHARGER)

    async def test_historical_record_remains_a_fallback_when_battery_is_unknown(self) -> None:
        self.c.data.update(protocol_version=7, battery=None)
        self.c._last_synced_session_ts = 1000
        await self.c._async_apply_session_record(_record(), None)
        self.assertEqual(self.c.data["battery"], 94)
        self.assertEqual(self.c.data["battery_source"], const.DATA_SOURCE_SESSION)
        self.assertIsNone(self.c.data["battery_updated_at"])

    async def test_missing_rtc_cannot_replace_known_current_battery(self) -> None:
        self.c.data["protocol_version"] = 7
        self.c._apply_battery_status(b"\x17", const.DATA_SOURCE_DIRECT)
        sampled_at = self.c.data["battery_updated_at"]
        await self.c._async_apply_session_record(_record(), None)
        self.assertEqual(self.c.data["battery"], 23)
        self.assertEqual(self.c.data["battery_updated_at"], sampled_at)
        self.assertEqual(self.c.data["battery_source"], const.DATA_SOURCE_DIRECT)

    async def test_rtc_before_record_end_cannot_date_a_new_battery_sample(self) -> None:
        self.c.data["protocol_version"] = 7
        self.c._last_synced_session_ts = 1000
        await self.c._async_apply_session_record(
            _record(), (1001).to_bytes(4, "little"),
            rtc_sampled_at=self.clock.utcnow(),
        )
        self.assertEqual(self.c.data["battery"], 99)
        self.assertEqual(self.c.data["battery_source"], "restored")

    async def test_historical_battery_timestamp_is_session_end_not_read_time(self) -> None:
        self.c.data.update(protocol_version=7, battery=None)
        self.c._last_synced_session_ts = 1000
        await self.c._async_apply_session_record(
            _record(), (1600).to_bytes(4, "little"),
            rtc_sampled_at=self.clock.utcnow(),
        )
        self.assertEqual(self.c.data["battery"], 94)
        self.assertEqual(self.c.data["battery_updated_at"],
                         self.clock.utcnow() - timedelta(seconds=480))

    async def test_recent_session_battery_can_replace_nine_day_old_restored_value(self) -> None:
        self.c.data["protocol_version"] = 7
        # A source label alone must not give a restored direct sample permanent
        # precedence over a reliably dated newer session measurement.
        self.c.data["battery_source"] = const.DATA_SOURCE_DIRECT
        self.c._last_synced_session_ts = 1000
        await self.c._async_apply_session_record(
            _record(), (1120).to_bytes(4, "little"),
            rtc_sampled_at=self.clock.utcnow(),
        )
        self.assertEqual(self.c.data["battery"], 94)

    async def test_restore_does_not_replace_fresh_sample_metadata(self) -> None:
        self.c.data["battery"] = None
        description = next(item for item in sensor.SENSORS if item.key == "battery")
        entity = sensor.OralBLiveSensor(self.c, description)
        async def restore():
            self.c._apply_battery_status(b"\x17", const.DATA_SOURCE_DIRECT)
            return types.SimpleNamespace(state="99", attributes={
                "last_read": "2026-09-22T12:00:00+00:00", "source": "retained_session",
            })
        entity.async_get_last_state = AsyncMock(side_effect=restore)
        with patch.object(sensor, "async_dispatcher_connect", return_value=lambda: None), \
             patch.object(entity, "async_write_ha_state"):
            await entity.async_added_to_hass()
        self.assertEqual(entity.native_value, 23)
        self.assertEqual(entity.extra_state_attributes["source"], const.DATA_SOURCE_DIRECT)
        self.assertEqual(entity.extra_state_attributes["last_read"], self.clock.utcnow())


if __name__ == "__main__":
    unittest.main()
