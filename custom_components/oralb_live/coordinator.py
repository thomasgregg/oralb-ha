"""Coordinator for Oral-B Live with two connection modes.

The brush accepts exactly ONE BLE client at a time and stops
advertising while that slot is taken. Whoever holds the slot gets the
data; everyone else -- the iO Sense charger, the phone app, Home
Assistant -- gets silence. That firmware property forces a choice,
exposed as the `connection_mode` option:

LIVE mode (the v0.4 behaviour): seize and hold the slot whenever it is
free, subscribe to the live notification characteristics, and stream
the 1 Hz brushing timer, pressure, quadrant and mode into the
entities. The trade-off: while we hold the slot, the iO Sense charger
display and the phone app do not work.

CHARGER-PRIORITY mode (default): never compete for the slot. During a
session the charger connects as designed and its lights/timer work.
When the brush is idle or docked again after a session -- it frees the
slot and resumes advertising within ~30 s -- we connect for a few
seconds, read the brush's own last-session record (ff29), the RTC,
battery and state, then disconnect. The trade-off: no live in-session
entities; the session appears about a minute after brushing ends,
timed by the brush itself.
"""

from __future__ import annotations

import asyncio
import logging
import time
from datetime import timedelta
from typing import Any

from bleak.backends.characteristic import BleakGATTCharacteristic
from bleak_retry_connector import (
    BleakClientWithServiceCache,
    BleakError,
    establish_connection,
)

from homeassistant.components import bluetooth
from homeassistant.components.bluetooth import (
    BluetoothChange,
    BluetoothScanningMode,
    BluetoothServiceInfoBleak,
)
from homeassistant.components.bluetooth.match import BluetoothCallbackMatcher
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.dispatcher import async_dispatcher_send
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util

from .const import (
    ADV_IDX_MODE,
    ADV_IDX_PRESSURE,
    ADV_IDX_SECTOR,
    ADV_IDX_STATE,
    ADV_IDX_TIME_HI,
    ADV_IDX_TIME_LO,
    AWAKE_STATES,
    CHAR_BRUSH_TIME,
    CHAR_MODE,
    CHAR_PACER,
    CHAR_PRESSURE,
    CHAR_RTC,
    CHAR_SECTOR,
    CHAR_SESSION_DATA,
    CHAR_STATE,
    CHAR_STATUS_BLOB,
    CONNECT_RETRIES,
    CONNECTION_MODE_LIVE,
    DOMAIN,
    MAX_SESSION_SECONDS,
    MODES,
    NOTIFY_CHARS,
    ORALB_MANUFACTURER_ID,
    PERIODIC_SYNC_INTERVAL_SECONDS,
    PRESSURE_FROM_ADV,
    PRESSURE_STATES,
    RECONNECT_INTERVAL_SECONDS,
    RELEASE_GRACE_SECONDS,
    RELEASE_STATES,
    RUNNING_STATE,
    SECTOR_NO_SECTOR,
    SESSION_RECORD_SETTLE_SECONDS,
    SESSION_SEEN_STATES,
    SIGNAL_UPDATE,
    STALE_CONNECTION_SECONDS,
    STATES,
    STORAGE_VERSION,
    SYNC_MIN_INTERVAL_SECONDS,
    SYNC_RETRY_ATTEMPTS,
    SYNC_RETRY_DELAY_SECONDS,
    SYNC_STATES,
)

_LOGGER = logging.getLogger(__name__)


def _decode_time(hi: int, lo: int) -> int:
    """Brush time frames are [minutes, seconds]."""
    return hi * 60 + lo


class OralBLiveCoordinator:
    """Owns all brush state and the BLE connection lifecycle."""

    def __init__(
        self, hass: HomeAssistant, address: str, name: str, mode: str
    ) -> None:
        self.hass = hass
        self.address = address
        self.name = name
        self.mode = mode
        self.data: dict[str, Any] = {
            "state": None,
            "state_raw": None,
            "time": None,
            "pressure": None,
            "mode": None,
            "mode_raw": None,
            "sector": None,
            "number_of_sectors": None,
            "battery": None,
            "rssi": None,
            "live": False,
            "connection_mode": mode,
            "last_session_start": None,
            "last_session_duration": None,
            "last_session_mode": None,
            "last_session_sectors": None,
            "last_session_high_pressure": None,
            "sessions_today": None,
        }
        self.available = False
        self._client: BleakClientWithServiceCache | None = None
        self._connect_lock = asyncio.Lock()
        self._connect_task: asyncio.Task | None = None
        self._disconnect_task: asyncio.Task | None = None
        self._reconnect_task: asyncio.Task | None = None
        self._last_activity = 0.0
        self._unsub_bluetooth: callback | None = None
        self._unsub_unavailable: callback | None = None
        self._stopping = False
        # --- session tracking (live mode / passive adverts) ---
        self._session_active = False
        self._session_start: Any = None
        self._session_max_time = 0
        self._session_sectors: set[int] = set()
        self._session_high_pressure = 0
        self._last_pressure: str | None = None
        # --- charger-priority sync state ---
        self._sync_task: asyncio.Task | None = None
        self._session_pending_sync = True  # seed on startup
        self._last_sync_attempt = 0.0
        self._last_sync_ok = 0.0
        self._last_synced_session_ts: int | None = None
        self._session_observed = False
        slug = address.replace(":", "").replace("-", "_").lower()
        self._store: Store = Store(hass, STORAGE_VERSION, f"{DOMAIN}.{slug}")

    # ------------------------------------------------------------------ setup
    @callback
    def async_start(self) -> None:
        """Register passive advertisement listeners."""
        self._unsub_bluetooth = bluetooth.async_register_callback(
            self.hass,
            self._async_advertisement,
            BluetoothCallbackMatcher(address=self.address),
            BluetoothScanningMode.PASSIVE,
        )
        self._unsub_unavailable = bluetooth.async_track_unavailable(
            self.hass, self._async_unavailable, self.address, connectable=False
        )
        # Seed from the most recent advertisement, if any.
        if service_info := bluetooth.async_last_service_info(
            self.hass, self.address, connectable=False
        ):
            self._parse_advertisement(service_info)
        if self.mode == CONNECTION_MODE_LIVE:
            # The brush stops advertising while a client (for example an
            # iO Sense charger) holds its single slot, so advertisements
            # alone are not a reliable trigger. Poll for a connection.
            self._reconnect_task = self.hass.async_create_task(
                self._reconnect_loop()
            )

    async def async_stop(self) -> None:
        self._stopping = True
        for task in (self._reconnect_task, self._sync_task):
            if task:
                task.cancel()
        self._reconnect_task = None
        self._sync_task = None
        if self._unsub_bluetooth:
            self._unsub_bluetooth()
            self._unsub_bluetooth = None
        if self._unsub_unavailable:
            self._unsub_unavailable()
            self._unsub_unavailable = None
        await self._async_disconnect()

    # -------------------------------------------------------------- passive
    @callback
    def _async_advertisement(
        self, service_info: BluetoothServiceInfoBleak, change: BluetoothChange
    ) -> None:
        self._parse_advertisement(service_info)

    def _parse_advertisement(self, service_info: BluetoothServiceInfoBleak) -> None:
        payload = service_info.manufacturer_data.get(ORALB_MANUFACTURER_ID)
        if not payload or len(payload) < 11:
            return
        self.available = True
        self.data["rssi"] = service_info.rssi
        state_raw = payload[ADV_IDX_STATE]
        self._apply_state(state_raw)
        # While live notifications flow, they are fresher than adverts.
        if not self.data["live"]:
            self.data["time"] = _decode_time(
                payload[ADV_IDX_TIME_HI], payload[ADV_IDX_TIME_LO]
            )
            self.data["pressure"] = PRESSURE_FROM_ADV.get(
                payload[ADV_IDX_PRESSURE], "normal"
            )
            self._track_session_time(self.data["time"])
            self._track_session_pressure(self.data["pressure"])
            self._apply_mode(payload[ADV_IDX_MODE])
            self._apply_sector(payload[ADV_IDX_SECTOR], None)
        self._push()

        if self._stopping:
            return
        if self.mode == CONNECTION_MODE_LIVE:
            if state_raw in AWAKE_STATES:
                self._schedule_connect()
        else:
            # Charger priority: never compete for the slot. Note that a
            # session happened (so a sync is due), and sync only in
            # quiet states -- which is also the only time the brush
            # advertises with a free slot anyway.
            if state_raw in SESSION_SEEN_STATES:
                self._session_observed = True
                self._session_pending_sync = True
            if state_raw in SYNC_STATES:
                self._maybe_schedule_sync()

    @callback
    def _async_unavailable(self, service_info: BluetoothServiceInfoBleak) -> None:
        self.available = False
        self._push()

    # --------------------------------------------------------------- active
    def _schedule_connect(self) -> None:
        if self._client and self._client.is_connected:
            return
        if self._connect_task and not self._connect_task.done():
            return
        self._connect_task = self.hass.async_create_task(self._async_connect())

    async def _async_connect(self) -> None:
        async with self._connect_lock:
            if self._client and self._client.is_connected:
                return
            ble_device = bluetooth.async_ble_device_from_address(
                self.hass, self.address, connectable=True
            )
            if ble_device is None:
                _LOGGER.debug("%s: no connectable path available", self.name)
                return
            try:
                client = await establish_connection(
                    BleakClientWithServiceCache,
                    ble_device,
                    self.name,
                    disconnected_callback=self._on_disconnect,
                    max_attempts=CONNECT_RETRIES,
                )
            except (BleakError, TimeoutError) as err:
                # The single slot is taken (charger or phone app).
                _LOGGER.debug(
                    "%s: could not win connection (%s); staying passive",
                    self.name,
                    err,
                )
                return
            self._client = client
            self._last_activity = time.monotonic()
            try:
                await self._async_subscribe(client)
                await self._async_initial_reads(client)
            except (BleakError, TimeoutError) as err:
                _LOGGER.debug("%s: setup after connect failed: %s", self.name, err)
                await self._async_disconnect()
                return
            self.data["live"] = True
            self._push()
            _LOGGER.debug("%s: live notifications active", self.name)
            self._watchdog()

    async def _async_subscribe(self, client: BleakClientWithServiceCache) -> None:
        for char_uuid in NOTIFY_CHARS:
            await client.start_notify(char_uuid, self._on_notify)

    async def _async_initial_reads(
        self, client: BleakClientWithServiceCache
    ) -> None:
        try:
            status = await client.read_gatt_char(CHAR_STATUS_BLOB)
            if status and 0 <= status[0] <= 100:
                self.data["battery"] = status[0]
        except (BleakError, TimeoutError):
            _LOGGER.debug("%s: battery read failed", self.name)
        try:
            pacer = await client.read_gatt_char(CHAR_PACER)
            sectors = len([b for b in pacer if b])
            if sectors:
                self.data["number_of_sectors"] = sectors
        except (BleakError, TimeoutError):
            pass
        try:
            t = await client.read_gatt_char(CHAR_BRUSH_TIME)
            if len(t) >= 2:
                self.data["time"] = _decode_time(t[0], t[1])
        except (BleakError, TimeoutError):
            pass

    def _on_notify(
        self, char: BleakGATTCharacteristic, payload: bytearray
    ) -> None:
        self._last_activity = time.monotonic()
        uuid = str(char.uuid)
        if uuid == CHAR_BRUSH_TIME and len(payload) >= 2:
            self.data["time"] = _decode_time(payload[0], payload[1])
            self._track_session_time(self.data["time"])
        elif uuid == CHAR_STATE and payload:
            self._apply_state(payload[0])
            if payload[0] in RELEASE_STATES:
                self._schedule_release()
        elif uuid == CHAR_MODE and payload:
            self._apply_mode(payload[0])
        elif uuid == CHAR_SECTOR and payload:
            self._apply_sector(payload[0], None)
        elif uuid == CHAR_PRESSURE and payload:
            self.data["pressure"] = PRESSURE_STATES.get(
                payload[0], f"pressure_{payload[0]}"
            )
            self._track_session_pressure(self.data["pressure"])
        self._push()

    # ------------------------------------------------- charger-priority sync
    def _maybe_schedule_sync(self) -> None:
        """Rate-limited trigger for a post-session / periodic sync."""
        if self._sync_task and not self._sync_task.done():
            return
        now = time.monotonic()
        if now - self._last_sync_attempt < SYNC_MIN_INTERVAL_SECONDS:
            return
        due = (
            self._session_pending_sync
            or self._last_sync_ok == 0.0
            or now - self._last_sync_ok > PERIODIC_SYNC_INTERVAL_SECONDS
        )
        if not due:
            return
        self._sync_task = self.hass.async_create_task(self._async_sync_sequence())

    async def _async_sync_sequence(self) -> None:
        """Wait for ff29 to settle, then retry stale post-session records."""
        attempts = SYNC_RETRY_ATTEMPTS if self._session_observed else 1
        if self._session_observed:
            _LOGGER.debug(
                "%s: waiting %ss for the session record to settle",
                self.name,
                SESSION_RECORD_SETTLE_SECONDS,
            )
            await asyncio.sleep(SESSION_RECORD_SETTLE_SECONDS)

        for attempt in range(1, attempts + 1):
            self._last_sync_attempt = time.monotonic()
            result = await self._async_sync_once()
            if result == "new":
                self._session_pending_sync = False
                self._session_observed = False
                return
            if not self._session_observed:
                # Startup and periodic probes do not imply a new session.
                self._session_pending_sync = False
                return
            if attempt < attempts:
                _LOGGER.debug(
                    "%s: session record %s; retrying in %ss (%s/%s)",
                    self.name,
                    result,
                    SYNC_RETRY_DELAY_SECONDS,
                    attempt,
                    attempts,
                )
                await asyncio.sleep(SYNC_RETRY_DELAY_SECONDS)

        _LOGGER.debug(
            "%s: no new session record after %s attempts; giving up until "
            "another session is observed",
            self.name,
            attempts,
        )
        self._session_pending_sync = False
        self._session_observed = False

    async def _async_sync_once(self) -> str:
        """Connect briefly, read the last-session record, disconnect.

        Total connected time is a few seconds -- far below the brush's
        own ~30 s idle timeout, and only ever in quiet states, so the
        charger and app never notice us.
        """
        async with self._connect_lock:
            if self._stopping:
                return
            ble_device = bluetooth.async_ble_device_from_address(
                self.hass, self.address, connectable=True
            )
            if ble_device is None:
                _LOGGER.debug("%s: sync skipped, no connectable path", self.name)
                return "failed"
            try:
                client = await establish_connection(
                    BleakClientWithServiceCache,
                    ble_device,
                    self.name,
                    max_attempts=2,
                )
            except (BleakError, TimeoutError) as err:
                _LOGGER.debug("%s: sync connect failed: %s", self.name, err)
                return "failed"
            try:
                record = await self._async_sync_read(
                    client, CHAR_SESSION_DATA, "session record"
                )
                rtc = await self._async_sync_read(client, CHAR_RTC, "RTC")
                status = await self._async_sync_read(
                    client, CHAR_STATUS_BLOB, "status"
                )
                state = await self._async_sync_read(client, CHAR_STATE, "state")
            finally:
                try:
                    await client.disconnect()
                except (BleakError, TimeoutError):
                    pass
        if status and 0 <= status[0] <= 100:
            self.data["battery"] = status[0]
        if state:
            self._apply_state(state[0])
        result = "missing"
        if record is not None:
            result = await self._async_apply_session_record(record, rtc)
        if any(value is not None for value in (record, rtc, status, state)):
            self._last_sync_ok = time.monotonic()
        self._push()
        _LOGGER.debug("%s: sync complete (session record: %s)", self.name, result)
        return result

    async def _async_sync_read(
        self,
        client: BleakClientWithServiceCache,
        char_uuid: str,
        label: str,
    ) -> bytearray | None:
        """Read one sync characteristic without suppressing later reads."""
        try:
            return await client.read_gatt_char(char_uuid)
        except (BleakError, TimeoutError) as err:
            _LOGGER.debug("%s: %s read failed: %s", self.name, label, err)
            return None

    async def _async_apply_session_record(
        self, record: bytes | bytearray, rtc: bytes | bytearray | None
    ) -> str:
        """Parse the ff29 last-session record and log new sessions.

        Layout (23 bytes, little-endian, protocol 8):
          [0:4]   session start, seconds on the brush clock
          [4:6]   session counter (tentative)
          [6:8]   target duration, seconds
          [8:10]  session duration, seconds
          [10:12] pressure-related count (tentative)
          [12:14] per-quadrant time, seconds
          [19]    brushing mode
          [20]    battery percent at session end

        The brush clock drifts (observed ~8 days off wall time), so the
        start time is derived RELATIVE to the RTC read in the same
        connection: wall_start = now - (rtc - record_ts). The raw
        record timestamp is used only for deduplication, persisted so
        restarts do not double-count.
        """
        if len(record) < 20:
            _LOGGER.debug(
                "%s: unexpected ff29 length %s: %s",
                self.name,
                len(record),
                bytes(record).hex(" "),
            )
            return "invalid"
        session_ts = int.from_bytes(record[0:4], "little")
        duration = int.from_bytes(record[8:10], "little")
        mode_raw = record[19]
        if session_ts == 0 or not 0 < duration <= MAX_SESSION_SECONDS:
            _LOGGER.debug(
                "%s: invalid ff29 record (timestamp=%s, duration=%s): %s",
                self.name,
                session_ts,
                duration,
                bytes(record).hex(" "),
            )
            return "invalid"
        if self._last_synced_session_ts is None:
            stored = await self._store.async_load() or {}
            self._last_synced_session_ts = stored.get("last_session_ts", 0)
        if session_ts == self._last_synced_session_ts:
            _LOGGER.debug(
                "%s: ff29 still contains the previous session "
                "(timestamp=%s, duration=%ss)",
                self.name,
                session_ts,
                duration,
            )
            return "duplicate"

        rtc_now: int | None = None
        if rtc is not None and len(rtc) >= 4:
            rtc_now = int.from_bytes(rtc[0:4], "little")
        if rtc_now is not None and rtc_now >= session_ts:
            start = dt_util.utcnow() - timedelta(seconds=rtc_now - session_ts)
        else:
            start = dt_util.utcnow()

        today = dt_util.now().date()
        previous_start = self.data.get("last_session_start")
        count = self.data.get("sessions_today") or 0
        if (
            previous_start is not None
            and dt_util.as_local(previous_start).date() != today
        ):
            count = 0
        self.data["sessions_today"] = count + 1
        self.data["last_session_start"] = start
        self.data["last_session_duration"] = duration
        self.data["last_session_mode"] = MODES.get(mode_raw, f"mode_{mode_raw}")
        # Quadrant coverage / high-pressure events are not decoded from
        # the record yet; passive advert tracking may have filled them.
        self._last_synced_session_ts = session_ts
        await self._store.async_save({"last_session_ts": session_ts})
        _LOGGER.debug(
            "%s: synced session from brush: %ss, mode %s, started %s",
            self.name,
            duration,
            self.data["last_session_mode"],
            start,
        )
        return "new"

    # ---------------------------------------------------------- state maps
    def _apply_state(self, raw: int) -> None:
        previous = self.data.get("state_raw")
        self.data["state_raw"] = raw
        self.data["state"] = STATES.get(raw, f"unknown_state_{raw}")
        if raw == RUNNING_STATE and previous != RUNNING_STATE:
            self._begin_session()
        elif raw != RUNNING_STATE and previous == RUNNING_STATE:
            self._end_session()

    # ---------------------------------------------------------- sessions
    def _begin_session(self) -> None:
        """A brushing session just started."""
        self._session_active = True
        self._session_start = dt_util.utcnow()
        self._session_max_time = 0
        self._session_sectors = set()
        self._session_high_pressure = 0
        self._last_pressure = None
        _LOGGER.debug("%s: session started", self.name)

    def _end_session(self) -> None:
        """A brushing session just finished; record the result.

        In charger-priority mode this usually records nothing (the
        brush is silent while the charger holds it, so no duration
        accumulates); the authoritative record then arrives via the
        ff29 sync instead.
        """
        if not self._session_active:
            return
        self._session_active = False
        duration = self._session_max_time or 0
        if duration <= 0:
            _LOGGER.debug("%s: session ended with no duration; ignored", self.name)
            return

        today = dt_util.now().date()
        previous_start = self.data.get("last_session_start")
        count = self.data.get("sessions_today") or 0
        if previous_start is not None:
            previous_day = dt_util.as_local(previous_start).date()
            if previous_day != today:
                count = 0
        self.data["sessions_today"] = count + 1

        self.data["last_session_start"] = self._session_start
        self.data["last_session_duration"] = duration
        self.data["last_session_mode"] = self.data.get("mode")
        self.data["last_session_sectors"] = len(self._session_sectors)
        self.data["last_session_high_pressure"] = self._session_high_pressure
        _LOGGER.debug(
            "%s: session ended: %ss, mode %s, %s quadrants, %s high-pressure events",
            self.name,
            duration,
            self.data.get("mode"),
            len(self._session_sectors),
            self._session_high_pressure,
        )

    def _track_session_time(self, seconds: int) -> None:
        if self._session_active and seconds > self._session_max_time:
            self._session_max_time = seconds

    def _track_session_pressure(self, pressure: str) -> None:
        if (
            self._session_active
            and pressure == "high"
            and self._last_pressure != "high"
        ):
            self._session_high_pressure += 1
        self._last_pressure = pressure

    def _apply_mode(self, raw: int) -> None:
        self.data["mode_raw"] = raw
        self.data["mode"] = MODES.get(raw, f"mode_{raw}")

    def _apply_sector(self, raw: int, total: int | None) -> None:
        if self._session_active and raw != SECTOR_NO_SECTOR:
            self._session_sectors.add(raw)
        if raw == SECTOR_NO_SECTOR:
            self.data["sector"] = "no_sector"
        else:
            self.data["sector"] = f"sector_{raw}"
        if total:
            self.data["number_of_sectors"] = total

    # ------------------------------------------------------------- teardown
    async def _reconnect_loop(self) -> None:
        """Live mode: keep trying to hold the connection.

        The brush has a single connection slot and stops advertising
        while any client holds it, so advertisement callbacks alone
        cannot trigger reconnects. It also drops idle clients after
        ~30 s of its own accord; _on_disconnect reconnects immediately
        when that happens, and this loop is the backstop.
        """
        try:
            while not self._stopping:
                await asyncio.sleep(RECONNECT_INTERVAL_SECONDS)
                if self._stopping:
                    return
                if self._client and self._client.is_connected:
                    continue
                if self.data.get("state_raw") in RELEASE_STATES:
                    continue
                self._schedule_connect()
        except asyncio.CancelledError:
            pass

    def _watchdog(self) -> None:
        async def _watch() -> None:
            while self._client and self._client.is_connected:
                await asyncio.sleep(30)
                if (
                    time.monotonic() - self._last_activity
                    > STALE_CONNECTION_SECONDS
                ):
                    _LOGGER.debug(
                        "%s: connection stale, rebuilding", self.name
                    )
                    await self._async_disconnect()
                    return

        self.hass.async_create_task(_watch())

    def _schedule_release(self) -> None:
        if self._disconnect_task and not self._disconnect_task.done():
            return

        async def _release() -> None:
            await asyncio.sleep(RELEASE_GRACE_SECONDS)
            await self._async_disconnect()

        self._disconnect_task = self.hass.async_create_task(_release())

    def _on_disconnect(self, _client: BleakClientWithServiceCache) -> None:
        self.data["live"] = False
        self._client = None
        if self._stopping:
            return
        self.hass.loop.call_soon_threadsafe(self._push)
        if (
            self.mode == CONNECTION_MODE_LIVE
            and self.data.get("state_raw") not in RELEASE_STATES
        ):
            # The brush drops idle clients after ~30 s. Reconnect
            # straight away (instead of waiting for the loop) so a
            # session never starts while the slot sits free.
            self.hass.loop.call_soon_threadsafe(self._schedule_connect)

    async def _async_disconnect(self) -> None:
        client, self._client = self._client, None
        self.data["live"] = False
        if client and client.is_connected:
            try:
                await client.disconnect()
            except (BleakError, TimeoutError):
                pass
        self._push()

    # --------------------------------------------------------------- update
    def _push(self) -> None:
        async_dispatcher_send(
            self.hass, f"{SIGNAL_UPDATE}_{self.address}", self.data
        )
