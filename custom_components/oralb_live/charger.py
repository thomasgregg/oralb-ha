"""Automatic iO Sense discovery and read-only toothbrush bridge."""

from __future__ import annotations

import asyncio
import logging
import time
from typing import TYPE_CHECKING, Any

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
from homeassistant.core import callback
from homeassistant.helpers.dispatcher import async_dispatcher_send

from .charger_protocol import (
    CHARGER_SNAPSHOT_COMMANDS,
    IOSENSE_COMMAND_UUID,
    IOSENSE_PROTOCOL_END,
    IOSENSE_READ_UUID,
    IOSENSE_SERVICE_UUID,
    IOSENSE_STATUS_UUID,
    IOSENSE_WRITE_UUID,
    ChargerCommand,
    ChargerPacket,
    build_charger_get,
    build_passthrough_read,
    charger_live_auxiliary,
    decode_charger_advertisement,
    decode_charger_read,
    normalize_mac,
    resolve_charger_session_running,
)
from .const import (
    CHARGER_ACTIVE_PROBE_INTERVAL_SECONDS,
    CHARGER_BATTERY_EVERY_TICKS,
    CHARGER_BRIDGE_INTERVAL_SECONDS,
    CHARGER_BRIDGE_REQUEST_TIMEOUT_SECONDS,
    CHARGER_IDLE_DISCONNECT_SECONDS,
    CHARGER_IDLE_PROBE_INTERVAL_SECONDS,
    CHARGER_POST_SESSION_READS,
    CHARGER_BRUSH_STATUS_EVERY_TICKS,
    CHARGER_SESSION_SYNC_INTERVAL_SECONDS,
    CHARGER_SNAPSHOT_INTERVAL_SECONDS,
    DATA_SOURCE_CHARGER,
    ORALB_MANUFACTURER_ID,
    SIGNAL_CHARGER_DISCOVERED,
    SIGNAL_CHARGER_UPDATE,
)

if TYPE_CHECKING:
    from .coordinator import OralBLiveCoordinator


_LOGGER = logging.getLogger(__name__)

_CHARGER_DATA_KEYS: dict[ChargerCommand, str] = {
    ChargerCommand.FW_VERSION: "firmware",
    ChargerCommand.HW_VERSION: "hardware_version",
    ChargerCommand.SERVER_MODE: "server_mode",
    ChargerCommand.DEVICE_ID: "device_id",
    ChargerCommand.WIFI_STATUS: "wifi_status",
    ChargerCommand.WIFI_RSSI: "wifi_rssi",
    ChargerCommand.IOT_STATUS: "cloud_status",
    ChargerCommand.INTERNET_TYPE: "internet_type",
    ChargerCommand.TIMEZONE: "timezone",
    ChargerCommand.UPTIME: "uptime",
    ChargerCommand.CLOCK_BRIGHTNESS: "clock_brightness",
    ChargerCommand.CLOCK_DISPLAY_MODE: "clock_mode",
    ChargerCommand.CLOCK_TEXT: "clock_text",
    ChargerCommand.DATE_SHOW_MODE: "date_show_mode",
    ChargerCommand.RING_COLOR: "ring_color",
    ChargerCommand.NIGHT_LIGHT_MODE: "night_light_mode",
    ChargerCommand.AUTO_UPDATE: "auto_update",
    ChargerCommand.BRUSH_CONNECTION_POLICY: "brush_connection_policy",
    ChargerCommand.SESSION_STATUS: "session_status",
    ChargerCommand.TOUCHPAD_STATUS: "touchpad_status",
    ChargerCommand.BRUSH_PAIRED: "brush_paired",
    ChargerCommand.BRUSH_STATUS: "brush_status",
}


class IOSenseBridge:
    """Use the charger as the brush's BLE owner and local read bridge."""

    def __init__(self, parent: OralBLiveCoordinator) -> None:
        self.parent = parent
        self.hass = parent.hass
        self.address: str | None = None
        self.name = "iO Sense Charger"
        self.data: dict[str, Any] = {
            "state": "unavailable",
            "address": None,
            "mac": None,
            "rssi": None,
            "firmware": None,
            "hardware_version": None,
            "body_color": None,
            "server_mode": None,
            "device_id": None,
            "wifi_status": None,
            "wifi_rssi": None,
            "internet_type": None,
            "cloud_status": None,
            "timezone": None,
            "uptime": None,
            "clock_brightness": None,
            "clock_mode": None,
            "clock_text": None,
            "date_show_mode": None,
            "ring_color": None,
            "night_light_mode": None,
            "auto_update": None,
            "touchpad_status": None,
            "brush_connection_policy": None,
            "brush_status": None,
            "session_status": None,
            "brush_paired": None,
            "brush_connected": False,
            "brush_charging": False,
            "internet_connected": None,
            "cloud_connected": None,
            "bridge_connected": False,
            "last_response_latency_ms": None,
        }
        self.available = False
        self._client: BleakClientWithServiceCache | None = None
        self._unsub_bluetooth: callback | None = None
        self._unsub_unavailable: callback | None = None
        self._connect_task: asyncio.Task | None = None
        self._live_task: asyncio.Task | None = None
        self._disconnect_task: asyncio.Task | None = None
        self._connect_lock = asyncio.Lock()
        self._request_lock = asyncio.Lock()
        self._pending_future: asyncio.Future | None = None
        self._pending_command: ChargerCommand | None = None
        self._pending_short_uuid: str | None = None
        self._candidate_addresses: set[str] = set()
        self._session_running = False
        self._snapshot_complete = False
        self._last_probe_monotonic = 0.0
        self._last_snapshot_monotonic = 0.0
        self._last_session_sync_monotonic = 0.0
        self._last_connection_bits: int | None = None
        self._stopping = False
        self._discovery_announced = False

    @property
    def session_running(self) -> bool:
        """Return whether this bridge currently owns an active brush session."""
        return self._session_running

    @callback
    def async_start(self) -> None:
        """Listen for any iO Sense and identify its paired brush locally."""
        self._unsub_bluetooth = bluetooth.async_register_callback(
            self.hass,
            self._async_advertisement,
            BluetoothCallbackMatcher(service_uuid=IOSENSE_SERVICE_UUID),
            BluetoothScanningMode.PASSIVE,
        )

    async def async_stop(self) -> None:
        self._stopping = True
        if self._unsub_bluetooth:
            self._unsub_bluetooth()
            self._unsub_bluetooth = None
        if self._unsub_unavailable:
            self._unsub_unavailable()
            self._unsub_unavailable = None
        for task in (self._connect_task, self._live_task, self._disconnect_task):
            if task:
                task.cancel()
        await self._async_disconnect()

    @callback
    def _async_advertisement(
        self, service_info: BluetoothServiceInfoBleak, change: BluetoothChange
    ) -> None:
        payload = service_info.manufacturer_data.get(ORALB_MANUFACTURER_ID)
        if payload is None:
            return
        decoded = decode_charger_advertisement(payload)
        if decoded.get("device_type") != "stargate":
            return
        if self.address is not None and service_info.address != self.address:
            return

        previous_bits = self._last_connection_bits
        current_bits = int(decoded["connection_bits"])
        self._last_connection_bits = current_bits
        self.available = True
        self.data.update(
            {
                "state": "connected" if self.data["bridge_connected"] else "available",
                "address": service_info.address,
                "mac": decoded.get("mac"),
                "rssi": service_info.rssi,
                "firmware": decoded.get("firmware"),
                "body_color": decoded.get("body_color"),
                "server_mode": decoded.get("server_mode"),
                "brush_connected": decoded.get("brush_connected", False),
                "brush_charging": decoded.get("brush_charging", False),
                "brush_paired": decoded.get("brush_paired"),
                "internet_connected": decoded.get("internet_connected"),
                "cloud_connected": decoded.get("cloud_connected"),
            }
        )
        if self.data["wifi_status"] is None:
            self.data["wifi_status"] = (
                "connected_with_internet"
                if decoded.get("internet_connected")
                else "not_connected"
            )
        if self.data["cloud_status"] is None:
            self.data["cloud_status"] = (
                "connected" if decoded.get("cloud_connected") else "not_connected"
            )
        self._push()

        if self.address is None:
            if service_info.address in self._candidate_addresses:
                return
            if self._connect_task and not self._connect_task.done():
                return
            self._candidate_addresses.add(service_info.address)
            self._schedule_connect(service_info.address)
        else:
            now = time.monotonic()
            brush_connected = bool(decoded.get("brush_connected"))
            brush_charging = bool(decoded.get("brush_charging"))
            state_changed = previous_bits is None or bool(
                (previous_bits ^ current_bits) & 0x18
            )
            probe_interval = (
                CHARGER_ACTIVE_PROBE_INTERVAL_SECONDS
                if brush_connected and not brush_charging
                else CHARGER_IDLE_PROBE_INTERVAL_SECONDS
            )
            if state_changed or now - self._last_probe_monotonic >= probe_interval:
                self._schedule_connect(self.address)

    @callback
    def _async_unavailable(self, _service_info: BluetoothServiceInfoBleak) -> None:
        self.available = False
        self.data["state"] = "unavailable"
        self._push()
        self.parent._push()

    def _schedule_connect(self, address: str) -> None:
        if self._stopping:
            return
        if self._connect_task and not self._connect_task.done():
            return
        self._connect_task = self.hass.async_create_task(self._async_connect(address))

    async def _async_connect(self, address: str) -> None:
        async with self._connect_lock:
            if self._stopping:
                return
            if self._client and self._client.is_connected:
                if self.address == address:
                    self._ensure_live_task()
                return
            self._last_probe_monotonic = time.monotonic()
            ble_device = bluetooth.async_ble_device_from_address(
                self.hass, address, connectable=True
            )
            if ble_device is None:
                _LOGGER.debug("iO Sense %s has no connectable path", address)
                self._candidate_addresses.discard(address)
                return
            try:
                client = await establish_connection(
                    BleakClientWithServiceCache,
                    ble_device,
                    self.name,
                    disconnected_callback=self._on_disconnect,
                    max_attempts=3,
                )
            except (BleakError, TimeoutError) as err:
                _LOGGER.debug("iO Sense %s connection failed: %s", address, err)
                self._candidate_addresses.discard(address)
                return

            self._client = client
            self.data["bridge_connected"] = True
            self.data["state"] = "connected"
            try:
                await client.start_notify(IOSENSE_READ_UUID, self._on_read)
                await client.start_notify(IOSENSE_STATUS_UUID, self._on_status)
                if self.address is None:
                    identity = await self._async_get(ChargerCommand.BRUSH_DATA)
                    identity_value = (
                        identity.value
                        if identity and isinstance(identity.value, dict)
                        else {}
                    )
                    paired_mac = identity_value.get("brush_mac")
                    if normalize_mac(paired_mac or "") != normalize_mac(
                        self.parent.address
                    ):
                        _LOGGER.debug(
                            "iO Sense %s belongs to brush %s, not %s",
                            address,
                            paired_mac,
                            self.parent.address,
                        )
                        await self._async_disconnect()
                        return
                    if all(
                        identity_value.get(key) is not None
                        for key in (
                            "model_id",
                            "protocol_version",
                            "firmware_revision",
                        )
                    ):
                        self.parent._apply_device_identity(
                            identity_value["model_id"],
                            identity_value["protocol_version"],
                            identity_value["firmware_revision"],
                            second_controller_version=identity_value.get(
                                "second_controller_version"
                            ),
                            media_content_version=identity_value.get(
                                "media_content_version"
                            ),
                        )
                    self.address = address
                    self.data["address"] = address
                    self._unsub_unavailable = bluetooth.async_track_unavailable(
                        self.hass,
                        self._async_unavailable,
                        address,
                        connectable=True,
                    )
                    self.parent._charger_discovered(self)
                    if not self._discovery_announced:
                        self._discovery_announced = True
                        async_dispatcher_send(
                            self.hass,
                            f"{SIGNAL_CHARGER_DISCOVERED}_{self.parent.address}",
                        )

                if self.data.get("brush_connected"):
                    await self._async_refresh_session_state()
                snapshot_due = (
                    not self._snapshot_complete
                    or time.monotonic() - self._last_snapshot_monotonic
                    >= CHARGER_SNAPSHOT_INTERVAL_SECONDS
                )
                if not self._session_running and snapshot_due:
                    await self._async_snapshot()
                if self._session_running:
                    self._ensure_live_task()
                else:
                    if self.data.get("brush_connected"):
                        await self._async_post_session_sync()
                    await self._async_disconnect()
            except (BleakError, TimeoutError, asyncio.TimeoutError) as err:
                _LOGGER.debug("iO Sense setup/read failed: %s", err)
                if self.address is None:
                    self._candidate_addresses.discard(address)
                await self._async_disconnect()
            finally:
                self._push()

    async def _async_snapshot(self) -> None:
        successful = 0
        for command in CHARGER_SNAPSHOT_COMMANDS:
            if self._stopping or self._session_running:
                return
            packet = await self._async_get(command)
            if packet is None:
                continue
            successful += 1
        self._snapshot_complete = successful > len(CHARGER_SNAPSHOT_COMMANDS) // 2
        if successful:
            self._last_snapshot_monotonic = time.monotonic()

    async def _async_refresh_session_state(self) -> None:
        """Read the two short charger values that gate live forwarding."""
        await self._async_get(ChargerCommand.SESSION_STATUS)
        # Always refresh BRUSH_STATUS as well. It is the reliable authority on
        # tested firmware and also clears a stale pre_run/run value after a
        # connection interruption.
        await self._async_get(ChargerCommand.BRUSH_STATUS)

    async def _async_post_session_sync(self, *, force: bool = False) -> None:
        """Collect retained brush data while the charger still owns the slot."""
        if not force and (
            time.monotonic() - self._last_session_sync_monotonic
            < CHARGER_SESSION_SYNC_INTERVAL_SECONDS
        ):
            return
        successful = 0
        for short_uuid in CHARGER_POST_SESSION_READS:
            if self._stopping or self._session_running:
                return
            if value := await self._async_passthrough(short_uuid):
                await self.parent._async_apply_charger_passthrough(short_uuid, value)
                successful += 1
        if successful:
            self._last_session_sync_monotonic = time.monotonic()

    async def _async_get(self, command: ChargerCommand) -> ChargerPacket | None:
        client = self._client
        if client is None or not client.is_connected:
            return None
        async with self._request_lock:
            loop = asyncio.get_running_loop()
            future = loop.create_future()
            self._pending_future = future
            self._pending_command = command
            self._pending_short_uuid = None
            started = loop.time()
            try:
                await client.write_gatt_char(
                    IOSENSE_COMMAND_UUID, build_charger_get(command), response=True
                )
                await client.write_gatt_char(
                    IOSENSE_COMMAND_UUID, IOSENSE_PROTOCOL_END, response=True
                )
                packet = await asyncio.wait_for(
                    future, timeout=CHARGER_BRIDGE_REQUEST_TIMEOUT_SECONDS
                )
                self.data["last_response_latency_ms"] = round(
                    (loop.time() - started) * 1000, 1
                )
                return packet
            except (BleakError, TimeoutError, asyncio.TimeoutError) as err:
                _LOGGER.debug("iO Sense GET %s failed: %s", command.name, err)
                return None
            finally:
                self._clear_pending(future)

    async def _async_passthrough(self, short_uuid: str) -> bytes | None:
        client = self._client
        if client is None or not client.is_connected:
            return None
        async with self._request_lock:
            loop = asyncio.get_running_loop()
            future = loop.create_future()
            self._pending_future = future
            self._pending_command = ChargerCommand.BRUSH_PASSTHROUGH
            self._pending_short_uuid = short_uuid
            started = loop.time()
            try:
                await client.write_gatt_char(
                    IOSENSE_COMMAND_UUID,
                    bytes((0xC1, ChargerCommand.BRUSH_PASSTHROUGH)),
                    response=True,
                )
                await client.write_gatt_char(
                    IOSENSE_WRITE_UUID,
                    build_passthrough_read(short_uuid),
                    response=True,
                )
                await client.write_gatt_char(
                    IOSENSE_COMMAND_UUID, IOSENSE_PROTOCOL_END, response=True
                )
                record = await asyncio.wait_for(
                    future, timeout=CHARGER_BRIDGE_REQUEST_TIMEOUT_SECONDS
                )
                self.data["last_response_latency_ms"] = round(
                    (loop.time() - started) * 1000, 1
                )
                return record["data"] if record.get("success") else None
            except (BleakError, TimeoutError, asyncio.TimeoutError) as err:
                _LOGGER.debug("iO Sense passthrough %s failed: %s", short_uuid, err)
                return None
            finally:
                self._clear_pending(future)

    def _clear_pending(self, future: asyncio.Future) -> None:
        if self._pending_future is future:
            self._pending_future = None
            self._pending_command = None
            self._pending_short_uuid = None
        if not future.done():
            future.cancel()

    def _on_read(self, _char: BleakGATTCharacteristic, payload: bytearray) -> None:
        packet = decode_charger_read(payload)
        self._apply_native_packet(packet)
        future = self._pending_future
        if future is not None and not future.done():
            if packet.command == ChargerCommand.BRUSH_PASSTHROUGH:
                if self._pending_command != ChargerCommand.BRUSH_PASSTHROUGH:
                    return
                for record in packet.value if isinstance(packet.value, list) else []:
                    if record.get("short_uuid") == self._pending_short_uuid:
                        future.set_result(record)
                        break
            elif packet.command == self._pending_command:
                future.set_result(packet)
        self._push()

    def _on_status(self, _char: BleakGATTCharacteristic, payload: bytearray) -> None:
        # Status packets acknowledge delivery; the read response carries the
        # actual value and is the only packet used to complete a request.
        _LOGGER.debug("iO Sense status: %s", bytes(payload).hex())

    def _apply_native_packet(self, packet: ChargerPacket) -> None:
        if packet.command in _CHARGER_DATA_KEYS:
            self.data[_CHARGER_DATA_KEYS[packet.command]] = packet.value
        if packet.command == ChargerCommand.BRUSH_STATUS:
            if packet.value == "not_connected":
                self.data["brush_connected"] = False
                self.data["brush_charging"] = False
            elif packet.value == "charging":
                self.data["brush_connected"] = True
                self.data["brush_charging"] = True
            elif packet.value in {"pre_run", "run", "idle"}:
                self.data["brush_connected"] = True
                self.data["brush_charging"] = False
        if packet.command in {
            ChargerCommand.SESSION_STATUS,
            ChargerCommand.BRUSH_STATUS,
        }:
            running = resolve_charger_session_running(
                self.data.get("session_status"),
                self.data.get("brush_status"),
                self._session_running,
            )
            if running:
                self._start_session()
            else:
                self._end_session()

    def _start_session(self) -> None:
        """Start or recover a charger-managed brush stream."""
        confirmed = (
            self.data.get("brush_status") == "run"
            or self.data.get("session_status") == "active_running"
        )
        self._cancel_disconnect()
        if not self._session_running:
            self._session_running = True
            self.parent._charger_session_started(confirmed=confirmed)
        elif not self.parent._session_active:
            # Re-enter tracking after a reload or missed state edge while the
            # charger still reports an active brush state.
            self.parent._charger_session_started(confirmed=confirmed)
        elif confirmed:
            self.parent._confirm_session()
        self._ensure_live_task()

    def _end_session(self) -> None:
        """Finish a charger-managed stream once the brush is genuinely quiet."""
        if not self._session_running:
            return
        self._session_running = False
        self.parent._charger_session_ended()
        self._schedule_disconnect()

    def _ensure_live_task(self) -> None:
        if not self._session_running:
            return
        if self._live_task and not self._live_task.done():
            return
        self._live_task = self.hass.async_create_task(self._async_live_loop())

    async def _async_live_loop(self) -> None:
        tick = 0
        mode_observed = False
        try:
            while (
                not self._stopping
                and self._session_running
                and self._client
                and self._client.is_connected
            ):
                started = time.monotonic()
                if pressure := await self._async_passthrough("FF0B"):
                    await self.parent._async_apply_charger_passthrough("FF0B", pressure)

                # Keep the proven two-read live schedule. The charger
                # serialises requests, so adding a third field here can make
                # the one-second pressure read miss its cycle. Slowly changing
                # values occupy one auxiliary slot at startup; timer and pacer
                # anchors alternate afterwards.
                auxiliary_action = charger_live_auxiliary(
                    tick,
                    mode_observed=mode_observed,
                    brush_status_every_ticks=CHARGER_BRUSH_STATUS_EVERY_TICKS,
                    battery_every_ticks=CHARGER_BATTERY_EVERY_TICKS,
                )
                if auxiliary_action == "BRUSH_STATUS":
                    # BRUSH_STATUS proved more reliable than SESSION_STATUS on
                    # the tested firmware. It occupies the auxiliary slot so
                    # pressure remains the first of only two serial requests.
                    await self._async_get(ChargerCommand.BRUSH_STATUS)
                    if not self._session_running:
                        break
                else:
                    if auxiliary := await self._async_passthrough(auxiliary_action):
                        await self.parent._async_apply_charger_passthrough(
                            auxiliary_action, auxiliary
                        )
                        if auxiliary_action == "FF07":
                            mode_observed = True

                self.parent._advance_charger_timer()
                self.parent.data["data_source"] = DATA_SOURCE_CHARGER
                self.parent.data["live"] = True
                self.parent._push()
                self._push()
                tick += 1
                remaining = CHARGER_BRIDGE_INTERVAL_SECONDS - (
                    time.monotonic() - started
                )
                if remaining > 0:
                    await asyncio.sleep(remaining)
        except asyncio.CancelledError:
            pass
        finally:
            self.parent.data["live"] = False
            self.parent._push()

    def _schedule_disconnect(self) -> None:
        if self._disconnect_task and not self._disconnect_task.done():
            return

        async def _release() -> None:
            if not self._session_running:
                # At active_idle the private charger-to-brush connection can
                # still exist for only a brief window. Read FF05 first (via
                # CHARGER_POST_SESSION_READS) before waiting to disconnect;
                # delaying the read made the battery remain at its pre-session
                # value on chargers that release the brush quickly.
                await self._async_post_session_sync(force=True)
            await asyncio.sleep(CHARGER_IDLE_DISCONNECT_SECONDS)
            if not self._session_running:
                await self._async_disconnect()

        self._disconnect_task = self.hass.async_create_task(_release())

    def _cancel_disconnect(self) -> None:
        if self._disconnect_task and not self._disconnect_task.done():
            self._disconnect_task.cancel()
        self._disconnect_task = None

    def _on_disconnect(self, _client: BleakClientWithServiceCache) -> None:
        self._client = None
        self.data["bridge_connected"] = False
        self.data["state"] = "available" if self.available else "unavailable"
        self.parent.data["live"] = False
        self.hass.loop.call_soon_threadsafe(self._push)
        self.hass.loop.call_soon_threadsafe(self.parent._push)

    async def _async_disconnect(self) -> None:
        client, self._client = self._client, None
        self.data["bridge_connected"] = False
        self.data["state"] = "available" if self.available else "unavailable"
        if client and client.is_connected:
            try:
                await client.disconnect()
            except (BleakError, TimeoutError):
                pass

    def _push(self) -> None:
        async_dispatcher_send(
            self.hass,
            f"{SIGNAL_CHARGER_UPDATE}_{self.parent.address}",
            self.data,
        )
