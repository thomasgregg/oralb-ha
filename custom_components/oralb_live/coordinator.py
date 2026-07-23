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
Passive running/quiet advertisements provide an immediate wall-clock
session fallback without taking the slot. When the brush is idle or
docked again, we also try briefly to read the brush's own last-session
record (ff29), RTC, battery, display face and supported diagnostics, then
disconnect. If the charger still owns the slot, the passive fallback remains
the session record; if the GATT read succeeds, it refines that same record
without counting it twice.
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
    ADV_IDX_FIRMWARE,
    ADV_IDX_MODE,
    ADV_IDX_MODEL,
    ADV_IDX_PRESSURE,
    ADV_IDX_PROTOCOL,
    ADV_IDX_SECTOR,
    ADV_IDX_SECTOR_TIMER,
    ADV_IDX_STATE,
    ADV_IDX_TIME_HI,
    ADV_IDX_TIME_LO,
    ADV_IDX_TOTAL_SECTORS,
    AWAKE_STATES,
    CHAR_AVAILABLE_MODES,
    CHAR_BRUSH_TIME,
    CHAR_MODE,
    CHAR_MODEL_ID,
    CHAR_PACER,
    CHAR_PRESSURE,
    CHAR_REFILL_REMAINDER,
    CHAR_RTC,
    CHAR_SECTOR,
    CHAR_SESSION_DATA,
    CHAR_SMILEY,
    CHAR_STATE,
    CHAR_STATUS_BLOB,
    CONNECT_RETRIES,
    CONNECTION_MODE_LIVE,
    DOMAIN,
    MAX_SESSION_SECONDS,
    MIN_PASSIVE_SESSION_SECONDS,
    MODEL_NAMES,
    MODES,
    NOTIFY_CHARS,
    OPTIONAL_NOTIFY_CHARS,
    ORALB_MANUFACTURER_ID,
    PASSIVE_SESSION_ACTIVE_STATES,
    PERIODIC_SYNC_INTERVAL_SECONDS,
    PRESSURE_FROM_ADV,
    PRESSURE_STATES,
    RECONNECT_INTERVAL_SECONDS,
    RELEASE_GRACE_SECONDS,
    RELEASE_STATES,
    RUNNING_STATE,
    REFILL_STATES,
    SESSION_RECONCILE_WINDOW_SECONDS,
    SESSION_RECORD_SETTLE_SECONDS,
    SESSION_SEEN_STATES,
    SIGNAL_UPDATE,
    SMILEYS,
    STALE_CONNECTION_SECONDS,
    STATES,
    STORAGE_VERSION,
    SYNC_MIN_INTERVAL_SECONDS,
    SYNC_RETRY_ATTEMPTS,
    SYNC_RETRY_DELAY_SECONDS,
    SYNC_STATES,
)
from .protocol import (
    decode_sector,
    parse_available_modes,
    parse_battery_status,
    parse_device_info,
    parse_pacer,
    parse_refill_remainder,
)

_LOGGER = logging.getLogger(__name__)


def _decode_time(hi: int, lo: int) -> int:
    """Brush time frames are [minutes, seconds]."""
    return hi * 60 + lo


class OralBLiveCoordinator:
    """Owns all brush state and the BLE connection lifecycle."""

    def __init__(self, hass: HomeAssistant, address: str, name: str, mode: str) -> None:
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
            "available_modes": None,
            "available_modes_raw": None,
            "sector": None,
            "sector_raw": None,
            "sector_timer": None,
            "number_of_sectors": None,
            "sector_times": None,
            "target_duration": None,
            "smiley": None,
            "smiley_raw": None,
            "battery": None,
            "battery_time_remaining": None,
            "battery_voltage": None,
            "battery_current": None,
            "battery_temperature": None,
            "refill_state": None,
            "refill_state_raw": None,
            "refill_days": None,
            "refill_brushing_time": None,
            "model_id": None,
            "model_name": None,
            "protocol_version": None,
            "firmware_revision": None,
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
        self._session_started_monotonic: float | None = None
        self._session_max_time = 0
        self._session_sectors: set[int] = set()
        self._session_high_pressure = 0
        self._last_pressure: str | None = None
        self._pending_passive_sessions: list[Any] = []
        # --- charger-priority sync state ---
        self._sync_task: asyncio.Task | None = None
        self._session_pending_sync = True  # seed on startup
        self._last_sync_attempt = 0.0
        self._last_sync_ok = 0.0
        self._last_synced_session_ts: int | None = None
        self._session_generation = 0
        self._processed_session_generation = 0
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
            self._reconnect_task = self.hass.async_create_task(self._reconnect_loop())

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
        self._apply_device_identity(
            payload[ADV_IDX_MODEL],
            payload[ADV_IDX_PROTOCOL],
            payload[ADV_IDX_FIRMWARE],
        )
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
            self.data["sector_timer"] = payload[ADV_IDX_SECTOR_TIMER]
            self._apply_sector(
                payload[ADV_IDX_SECTOR],
                (
                    payload[ADV_IDX_TOTAL_SECTORS]
                    if self.data["number_of_sectors"] is None
                    else None
                ),
            )
        self._push()

        if self._stopping:
            return
        if self.mode == CONNECTION_MODE_LIVE:
            if state_raw in AWAKE_STATES:
                self._schedule_connect()
        else:
            # Charger priority: never compete for the slot. Note that a
            # session happened in _apply_state(), and sync only in quiet
            # states -- which is also the only time the brush may have a
            # free connection slot.
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
        for char_uuid in OPTIONAL_NOTIFY_CHARS:
            try:
                await client.start_notify(char_uuid, self._on_notify)
            except (BleakError, TimeoutError) as err:
                _LOGGER.debug(
                    "%s: optional notification %s unavailable: %s",
                    self.name,
                    char_uuid[4:8],
                    err,
                )

    async def _async_initial_reads(self, client: BleakClientWithServiceCache) -> None:
        status = await self._async_sync_read(client, CHAR_STATUS_BLOB, "status")
        model_info = await self._async_sync_read(client, CHAR_MODEL_ID, "device info")
        pacer = await self._async_sync_read(client, CHAR_PACER, "pacer")
        available_modes = await self._async_sync_read(
            client, CHAR_AVAILABLE_MODES, "available modes"
        )
        refill = await self._async_sync_read(
            client, CHAR_REFILL_REMAINDER, "refill remainder"
        )
        smiley = await self._async_sync_read(client, CHAR_SMILEY, "smiley")
        brushing_time = await self._async_sync_read(
            client, CHAR_BRUSH_TIME, "brushing time"
        )

        self._apply_battery_status(status)
        self._apply_device_info(model_info)
        self._apply_pacer(pacer)
        self._apply_available_modes(available_modes)
        self._apply_refill(refill)
        self._apply_smiley(smiley)
        if brushing_time is not None and len(brushing_time) >= 2:
            self.data["time"] = _decode_time(brushing_time[0], brushing_time[1])

    def _on_notify(self, char: BleakGATTCharacteristic, payload: bytearray) -> None:
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
        elif uuid == CHAR_STATUS_BLOB:
            self._apply_battery_status(payload)
        elif uuid == CHAR_SMILEY:
            self._apply_smiley(payload)
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
        """Sync every session generation, including back-to-back sessions."""
        while not self._stopping:
            target_generation = self._session_generation
            session_observed = target_generation > self._processed_session_generation
            attempts = SYNC_RETRY_ATTEMPTS if session_observed else 1
            result = "failed"

            if session_observed:
                _LOGGER.debug(
                    "%s: waiting %ss for session generation %s to settle",
                    self.name,
                    SESSION_RECORD_SETTLE_SECONDS,
                    target_generation,
                )
                await asyncio.sleep(SESSION_RECORD_SETTLE_SECONDS)

            for attempt in range(1, attempts + 1):
                # A newer brushing session can begin during the settle/retry
                # window. Never connect until the latest advertisement is
                # quiet again.
                while (
                    not self._stopping and self.data.get("state_raw") not in SYNC_STATES
                ):
                    await asyncio.sleep(1)
                if self._stopping:
                    return

                self._last_sync_attempt = time.monotonic()
                result = await self._async_sync_once()
                if result == "new" or not session_observed:
                    break
                if attempt < attempts:
                    _LOGGER.debug(
                        "%s: generation %s session record %s; retrying in %ss (%s/%s)",
                        self.name,
                        target_generation,
                        result,
                        SYNC_RETRY_DELAY_SECONDS,
                        attempt,
                        attempts,
                    )
                    await asyncio.sleep(SYNC_RETRY_DELAY_SECONDS)

            if session_observed:
                self._processed_session_generation = target_generation
                if result != "new":
                    _LOGGER.debug(
                        "%s: no new record for session generation %s after "
                        "%s attempts; keeping the passive session",
                        self.name,
                        target_generation,
                        attempts,
                    )

            if self._session_generation > target_generation:
                _LOGGER.debug(
                    "%s: newer session generation %s arrived while syncing "
                    "%s; continuing",
                    self.name,
                    self._session_generation,
                    target_generation,
                )
                continue

            self._session_pending_sync = False
            return

    async def _async_sync_once(self) -> str:
        """Connect briefly, read the last-session record, disconnect.

        Total connected time is a few seconds -- far below the brush's
        own ~30 s idle timeout, and only ever in quiet states, so the
        charger and app never notice us.
        """
        async with self._connect_lock:
            if self._stopping:
                return "failed"
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
                status = await self._async_sync_read(client, CHAR_STATUS_BLOB, "status")
                state = await self._async_sync_read(client, CHAR_STATE, "state")
                smiley = await self._async_sync_read(client, CHAR_SMILEY, "smiley")
                refill = await self._async_sync_read(
                    client, CHAR_REFILL_REMAINDER, "refill remainder"
                )
                model_info = (
                    await self._async_sync_read(client, CHAR_MODEL_ID, "device info")
                    if self.data["model_id"] is None
                    else None
                )
                pacer = (
                    await self._async_sync_read(client, CHAR_PACER, "pacer")
                    if self.data["sector_times"] is None
                    else None
                )
                available_modes = (
                    await self._async_sync_read(
                        client, CHAR_AVAILABLE_MODES, "available modes"
                    )
                    if self.data["available_modes"] is None
                    else None
                )
            finally:
                try:
                    await client.disconnect()
                except (BleakError, TimeoutError):
                    pass
        self._apply_battery_status(status)
        self._apply_smiley(smiley)
        self._apply_refill(refill)
        self._apply_device_info(model_info)
        self._apply_pacer(pacer)
        self._apply_available_modes(available_modes)
        if state:
            self._apply_state(state[0])
        result = "missing"
        if record is not None:
            result = await self._async_apply_session_record(record, rtc)
        if any(
            value is not None
            for value in (
                record,
                rtc,
                status,
                state,
                smiley,
                refill,
                model_info,
                pacer,
                available_modes,
            )
        ):
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
        matched_passive_start = None
        if self._pending_passive_sessions:
            matched_passive_start = min(
                self._pending_passive_sessions,
                key=lambda candidate: abs((start - candidate).total_seconds()),
            )
            if (
                abs((start - matched_passive_start).total_seconds())
                > SESSION_RECONCILE_WINDOW_SECONDS
            ):
                matched_passive_start = None
        reconciles_passive_session = matched_passive_start is not None
        if reconciles_passive_session:
            self._pending_passive_sessions.remove(matched_passive_start)
        elif previous_start is not None:
            # After an integration reload only the most recent passive session
            # is restored, not the in-memory queue.
            reconciles_passive_session = (
                abs((start - previous_start).total_seconds())
                <= SESSION_RECONCILE_WINDOW_SECONDS
            )

        if not reconciles_passive_session:
            count = self.data.get("sessions_today") or 0
            if (
                previous_start is not None
                and dt_util.as_local(previous_start).date() != today
            ):
                count = 0
            self.data["sessions_today"] = count + 1

        updates_latest_session = (
            previous_start is None
            or matched_passive_start == previous_start
            or (matched_passive_start is None and reconciles_passive_session)
            or start >= previous_start
        )
        if updates_latest_session:
            self.data["last_session_start"] = start
            self.data["last_session_duration"] = duration
            self.data["last_session_mode"] = MODES.get(mode_raw, f"mode_{mode_raw}")
            # Quadrant coverage / high-pressure events are not decoded from
            # the record yet; passive advert tracking may have filled them.
        self._last_synced_session_ts = session_ts
        await self._store.async_save({"last_session_ts": session_ts})
        if reconciles_passive_session:
            if updates_latest_session:
                _LOGGER.debug(
                    "%s: reconciled passive session with brush record: "
                    "%ss, mode %s, started %s",
                    self.name,
                    duration,
                    self.data["last_session_mode"],
                    start,
                )
            else:
                _LOGGER.debug(
                    "%s: reconciled older passive session started %s without "
                    "replacing newer session %s",
                    self.name,
                    start,
                    previous_start,
                )
        else:
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

        session_states = (
            PASSIVE_SESSION_ACTIVE_STATES
            if self.mode != CONNECTION_MODE_LIVE
            else {RUNNING_STATE}
        )
        if raw in session_states and previous not in session_states:
            self._begin_session()
        elif raw not in session_states and previous in session_states:
            self._end_session()

        if (
            self.mode != CONNECTION_MODE_LIVE
            and raw in SESSION_SEEN_STATES
            and previous not in SESSION_SEEN_STATES
        ):
            self._session_generation += 1
            self._session_pending_sync = True
            _LOGGER.debug(
                "%s: observed session generation %s in state %s",
                self.name,
                self._session_generation,
                self.data["state"],
            )

    # ---------------------------------------------------------- sessions
    def _begin_session(self) -> None:
        """A brushing session just started."""
        self._session_active = True
        self._session_start = dt_util.utcnow()
        self._session_started_monotonic = time.monotonic()
        self._session_max_time = 0
        self._session_sectors = set()
        self._session_high_pressure = 0
        self._last_pressure = None
        _LOGGER.debug("%s: session started", self.name)

    def _end_session(self) -> None:
        """A brushing session just finished; record the result.

        Prefer the brush's advertised/notified timer. If it stays at
        zero, use elapsed wall time between the observed running and
        quiet states. This passive fallback needs no connection and is
        later reconciled with ff29 if that record becomes available.
        """
        if not self._session_active:
            return
        self._session_active = False
        duration = self._session_max_time or 0
        duration_source = "brush timer"
        if duration <= 0 and self._session_started_monotonic is not None:
            duration = round(time.monotonic() - self._session_started_monotonic)
            duration_source = "passive elapsed time"
        self._session_started_monotonic = None
        if duration <= 0:
            _LOGGER.debug("%s: session ended with no duration; ignored", self.name)
            return
        if duration > MAX_SESSION_SECONDS:
            _LOGGER.debug(
                "%s: session duration %ss exceeds the maximum; ignored",
                self.name,
                duration,
            )
            return
        if (
            duration_source == "passive elapsed time"
            and duration < MIN_PASSIVE_SESSION_SECONDS
        ):
            _LOGGER.debug(
                "%s: %ss passive session is shorter than the %ss minimum; ignored",
                self.name,
                duration,
                MIN_PASSIVE_SESSION_SECONDS,
            )
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
        self._pending_passive_sessions.append(self._session_start)
        self._pending_passive_sessions = self._pending_passive_sessions[-10:]
        _LOGGER.debug(
            "%s: session ended from %s: %ss, mode %s, %s quadrants, "
            "%s high-pressure events",
            self.name,
            duration_source,
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
        self.data["sector_raw"] = raw
        sector, quadrant, decoded_total = decode_sector(
            raw,
            total,
            self.data.get("number_of_sectors"),
        )
        self.data["sector"] = sector
        if self._session_active and quadrant is not None:
            self._session_sectors.add(quadrant)
        if decoded_total:
            self.data["number_of_sectors"] = decoded_total

    def _apply_battery_status(self, payload: bytes | bytearray | None) -> None:
        if payload is not None:
            self.data.update(parse_battery_status(payload))

    def _apply_device_info(self, payload: bytes | bytearray | None) -> None:
        if payload is None:
            return
        parsed = parse_device_info(payload)
        if parsed:
            self._apply_device_identity(
                parsed["model_id"],
                parsed["protocol_version"],
                parsed["firmware_revision"],
            )

    def _apply_device_identity(
        self, model_id: int, protocol_version: int, firmware_revision: int
    ) -> None:
        self.data["model_id"] = model_id
        self.data["model_name"] = MODEL_NAMES.get(
            model_id, f"Oral-B model 0x{model_id:02x}"
        )
        self.data["protocol_version"] = protocol_version
        self.data["firmware_revision"] = firmware_revision

    def _apply_pacer(self, payload: bytes | bytearray | None) -> None:
        if payload is not None:
            self.data.update(parse_pacer(payload))

    def _apply_available_modes(self, payload: bytes | bytearray | None) -> None:
        if payload is None:
            return
        raw_modes = parse_available_modes(payload)
        if raw_modes:
            self.data["available_modes_raw"] = raw_modes
            self.data["available_modes"] = [
                MODES.get(raw, f"mode_{raw}") for raw in raw_modes
            ]

    def _apply_refill(self, payload: bytes | bytearray | None) -> None:
        if payload is None:
            return
        parsed = parse_refill_remainder(payload)
        if parsed:
            self.data.update(parsed)
            state_raw = parsed["refill_state_raw"]
            self.data["refill_state"] = REFILL_STATES.get(
                state_raw, f"state_{state_raw}"
            )

    def _apply_smiley(self, payload: bytes | bytearray | None) -> None:
        if not payload:
            return
        raw = payload[0]
        self.data["smiley_raw"] = raw
        self.data["smiley"] = SMILEYS.get(raw, f"face_{raw}")

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
                if time.monotonic() - self._last_activity > STALE_CONNECTION_SECONDS:
                    _LOGGER.debug("%s: connection stale, rebuilding", self.name)
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
        async_dispatcher_send(self.hass, f"{SIGNAL_UPDATE}_{self.address}", self.data)
