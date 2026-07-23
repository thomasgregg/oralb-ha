"""Hybrid passive/active coordinator for Oral-B Live.

Passive: listens to BLE advertisements (manufacturer id 0x00DC) exactly
like the official integration, at zero connection cost.

Active: the moment the brush reports an awake state, we establish a GATT
connection (through any connectable scanner, e.g. an ESPHome Bluetooth
proxy with active connections enabled) and subscribe to the live
notification characteristics -- restoring the 1 Hz brushing timer, live
pressure, mode and sector that recent iO firmwares no longer broadcast.

If the connection cannot be won (typically because the Oral-B phone app
or the iO Sense charger holds it), we degrade gracefully to passive mode
instead of fighting over the brush.
"""

from __future__ import annotations

import asyncio
import logging
import time
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
    CHAR_PRESSURE_EVENT,
    CHAR_SECTOR,
    CHAR_STATE,
    CHAR_STATUS_BLOB,
    CONNECT_RETRIES,
    IDLE_DISCONNECT_SECONDS,
    MODES,
    NOTIFY_CHARS,
    ORALB_MANUFACTURER_ID,
    PRESSURE_FROM_ADV,
    RELEASE_GRACE_SECONDS,
    RELEASE_STATES,
    SECTOR_NO_SECTOR,
    SIGNAL_UPDATE,
    STATES,
)

_LOGGER = logging.getLogger(__name__)


def _decode_time(hi: int, lo: int) -> int:
    """Brush time frames are [minutes, seconds]."""
    return hi * 60 + lo


class OralBLiveCoordinator:
    """Owns all brush state and the BLE connection lifecycle."""

    def __init__(self, hass: HomeAssistant, address: str, name: str) -> None:
        self.hass = hass
        self.address = address
        self.name = name
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
        }
        self.available = False
        self._client: BleakClientWithServiceCache | None = None
        self._connect_lock = asyncio.Lock()
        self._connect_task: asyncio.Task | None = None
        self._disconnect_task: asyncio.Task | None = None
        self._last_activity = 0.0
        self._unsub_bluetooth: callback | None = None
        self._unsub_unavailable: callback | None = None
        self._stopping = False

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

    async def async_stop(self) -> None:
        self._stopping = True
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
            self._apply_mode(payload[ADV_IDX_MODE])
            self._apply_sector(payload[ADV_IDX_SECTOR], None)
        self._push()

        if state_raw in AWAKE_STATES and not self._stopping:
            self._schedule_connect()

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
                # Most likely the phone app or iO Sense holds the brush.
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
        elif uuid == CHAR_STATE and payload:
            self._apply_state(payload[0])
            if payload[0] in RELEASE_STATES:
                self._schedule_release()
        elif uuid == CHAR_MODE and payload:
            self._apply_mode(payload[0])
        elif uuid == CHAR_SECTOR and payload:
            self._apply_sector(payload[0], None)
        elif uuid == CHAR_PRESSURE_EVENT:
            self.data["pressure"] = "high" if any(payload) else "normal"
        self._push()

    # ---------------------------------------------------------- state maps
    def _apply_state(self, raw: int) -> None:
        self.data["state_raw"] = raw
        self.data["state"] = STATES.get(raw, f"unknown_state_{raw}")

    def _apply_mode(self, raw: int) -> None:
        self.data["mode_raw"] = raw
        self.data["mode"] = MODES.get(raw, f"mode_{raw}")

    def _apply_sector(self, raw: int, total: int | None) -> None:
        if raw == SECTOR_NO_SECTOR:
            self.data["sector"] = "no_sector"
        else:
            self.data["sector"] = f"sector_{raw}"
        if total:
            self.data["number_of_sectors"] = total

    # ------------------------------------------------------------- teardown
    def _watchdog(self) -> None:
        async def _watch() -> None:
            while self._client and self._client.is_connected:
                await asyncio.sleep(15)
                if (
                    time.monotonic() - self._last_activity
                    > IDLE_DISCONNECT_SECONDS
                ):
                    _LOGGER.debug("%s: idle, releasing connection", self.name)
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
        if not self._stopping:
            self.hass.loop.call_soon_threadsafe(self._push)

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

