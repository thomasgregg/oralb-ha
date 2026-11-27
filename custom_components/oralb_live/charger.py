"""Automatic iO Sense discovery and read-only toothbrush bridge."""

from __future__ import annotations

import asyncio
import logging
import random
import time
from collections.abc import Callable
from dataclasses import dataclass
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
    IOSENSE_BRUSH_READ,
    IOSENSE_COMMAND_UUID,
    IOSENSE_GET,
    IOSENSE_POST,
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
    charger_session_confirmed,
    decode_charger_advertisement,
    decode_charger_read,
    normalize_mac,
    resolve_charger_session_running,
)
from .client_cleanup import ClientCleanup, is_current_client
from .const import (
    BLE_NOTIFY_TIMEOUT_SECONDS,
    CHARGER_ACTIVE_PROBE_INTERVAL_SECONDS,
    CHARGER_ACTIVE_RECONNECT_ATTEMPTS,
    CHARGER_BATTERY_EVERY_TICKS,
    CHARGER_BRIDGE_INTERVAL_SECONDS,
    CHARGER_BRIDGE_REQUEST_TIMEOUT_SECONDS,
    CHARGER_BRUSH_STATUS_EVERY_TICKS,
    CHARGER_CONTINUATION_TIMER_ATTEMPTS,
    CHARGER_DISCOVERY_RETRY_BASE_SECONDS,
    CHARGER_DISCOVERY_RETRY_BUSY_SECONDS,
    CHARGER_DISCOVERY_RETRY_MAX_SECONDS,
    CHARGER_IDLE_DISCONNECT_SECONDS,
    CHARGER_IDLE_PROBE_INTERVAL_SECONDS,
    CHARGER_LIVE_RESPONSE_TIMEOUT_SECONDS,
    CHARGER_POST_SESSION_READS,
    CHARGER_SESSION_SYNC_INTERVAL_SECONDS,
    CHARGER_SNAPSHOT_INTERVAL_SECONDS,
    DATA_SOURCE_CHARGER,
    ORALB_MANUFACTURER_ID,
    RUNNING_STATE,
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


_CHARGER_DEFAULTS: dict[str, Any] = {
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


@dataclass
class _ChargerCandidate:
    advertisement: BluetoothServiceInfoBleak
    bits: int
    retry_at: float | None
    failures: int = 0
    pairing_revision: int = 0
    unsub_unavailable: Callable[[], None] | None = None

    def stop_tracking(self) -> None:
        if self.unsub_unavailable is not None:
            self.unsub_unavailable()
            self.unsub_unavailable = None


class IOSenseBridge:
    """Use the charger as the brush's BLE owner and local read bridge."""

    def __init__(self, parent: OralBLiveCoordinator) -> None:
        self.parent = parent
        self.hass = parent.hass
        self.address: str | None = None
        self.name = "iO Sense Charger"
        self.data = _CHARGER_DEFAULTS.copy()
        self.available = False
        self._advertisement_available = False
        self._client: BleakClientWithServiceCache | None = None
        self._unsub_bluetooth: callback | None = None
        self._unsub_unavailable: callback | None = None
        self._connect_task: asyncio.Task | None = None
        self._live_task: asyncio.Task | None = None
        self._disconnect_task: asyncio.Task | None = None
        self._client_cleanup = ClientCleanup(self.hass)
        self._connect_lock = asyncio.Lock()
        self._request_lock = asyncio.Lock()
        self._pending_future: asyncio.Future | None = None
        self._pending_command: ChargerCommand | None = None
        self._pending_short_uuid: str | None = None
        self._pending_response_armed = False
        self._last_live_response_monotonic = 0.0
        self._candidates: dict[str, _ChargerCandidate] = {}
        self._retry_timer: asyncio.TimerHandle | None = None
        self._active_retries_remaining = 0
        self._stream_generation = 0
        self._session_running = False
        self._session_confirmed = False
        self._connection_brush_status: str | None = None
        self._connection_session_status: str | None = None
        self._provisional_timer_baseline: int | None = None
        self._snapshot_complete = False
        self._last_probe_monotonic = 0.0
        self._last_snapshot_monotonic = 0.0
        self._last_session_sync_monotonic = 0.0
        self._last_connection_bits: int | None = None
        self._stopping = False

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
        self._cancel_retry_timer()
        for candidate in self._candidates.values():
            candidate.stop_tracking()
        self._candidates.clear()
        if self._unsub_bluetooth:
            self._unsub_bluetooth()
            self._unsub_bluetooth = None
        if self._unsub_unavailable:
            self._unsub_unavailable()
            self._unsub_unavailable = None
        tasks = [
            task
            for task in (self._connect_task, self._live_task, self._disconnect_task)
            if task is not None
        ]
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        await self._async_disconnect()

    @callback
    def _async_advertisement(
        self, service_info: BluetoothServiceInfoBleak, change: BluetoothChange
    ) -> None:
        if self._stopping:
            return
        payload = service_info.manufacturer_data.get(ORALB_MANUFACTURER_ID)
        if payload is None:
            return
        decoded = decode_charger_advertisement(payload)
        if decoded.get("device_type") != "stargate":
            return

        address = service_info.address
        bits = int(decoded["connection_bits"])
        now = time.monotonic()
        candidate = self._candidates.get(address)
        paired_changed = candidate is not None and bool((candidate.bits ^ bits) & 0x40)
        if candidate is None:
            delay = CHARGER_DISCOVERY_RETRY_BUSY_SECONDS if self._connecting() else 0
            candidate = self._candidates[address] = _ChargerCandidate(
                service_info, bits, now + delay
            )
        else:
            candidate.advertisement = service_info
            candidate.bits = bits
            if paired_changed:
                candidate.pairing_revision += 1
                candidate.failures = 0
                candidate.retry_at = now if bits & 0x40 else None

        if self.address == address:
            returning = (
                not self._advertisement_available and not self._owns_client(self._client)
            )
            if returning and not paired_changed:
                candidate.failures = 0
                candidate.retry_at = now
            if paired_changed or returning:
                self._invalidate_association()
        if self.address != address and candidate.unsub_unavailable is None:
            self._track_candidate(address, candidate)
        if self.address is None:
            self._try_discovery()
            return
        if self.address != address:
            return

        previous_bits = self._last_connection_bits
        self._publish_advertisement(service_info, decoded)
        brush_active = bool(bits & 0x08) and not bool(bits & 0x10)
        state_changed = previous_bits is None or bool((previous_bits ^ bits) & 0x18)
        if self._active_retries_remaining:
            if brush_active or not state_changed:
                return
            self._active_retries_remaining = 0
            self._cancel_retry_timer()
        probe_interval = (
            CHARGER_ACTIVE_PROBE_INTERVAL_SECONDS
            if brush_active
            else CHARGER_IDLE_PROBE_INTERVAL_SECONDS
        )
        if state_changed or now - self._last_probe_monotonic >= probe_interval:
            self._schedule_connect(address)

    def _publish_advertisement(
        self, service_info: BluetoothServiceInfoBleak, decoded: dict[str, Any]
    ) -> None:
        """Publish only the advertisement of a verified charger."""
        was_available = self.available
        self._last_connection_bits = int(decoded["connection_bits"])
        self._advertisement_available = True
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
        if not was_available:
            self.parent._push()

    @callback
    def _async_unavailable(self, _service_info: BluetoothServiceInfoBleak) -> None:
        if self._stopping:
            return
        self._advertisement_available = False
        # Scanner history can expire while the verified GATT link still works.
        if self._owns_client(self._client):
            return
        self._active_retries_remaining = 0
        self._cancel_retry_timer()
        self._detach_client()

    def _invalidate_association(self) -> None:
        self.address = None
        self._advertisement_available = False
        self._active_retries_remaining = 0
        self._cancel_retry_timer()
        if self._unsub_unavailable:
            self._unsub_unavailable()
            self._unsub_unavailable = None
        self._detach_client()
        self._last_connection_bits = None
        self._last_snapshot_monotonic = 0.0
        self._last_session_sync_monotonic = 0.0
        self._snapshot_complete = False
        self.data.update(_CHARGER_DEFAULTS)
        self.parent._charger_unmatched()
        self._push()

    def _connecting(self) -> bool:
        return self._connect_task is not None and not self._connect_task.done()

    def _cancel_retry_timer(self) -> None:
        if self._retry_timer is not None:
            self._retry_timer.cancel()
            self._retry_timer = None

    def _track_candidate(self, address: str, candidate: _ChargerCandidate) -> None:
        @callback
        def unavailable(_service_info: BluetoothServiceInfoBleak) -> None:
            if self._candidates.get(address) is candidate:
                self._forget_candidate(address)
                self._try_discovery()

        candidate.unsub_unavailable = bluetooth.async_track_unavailable(
            self.hass, unavailable, address, connectable=True
        )

    def _forget_candidate(self, address: str) -> None:
        self._candidates.pop(address).stop_tracking()

    @callback
    def _discovery_wakeup(self) -> None:
        self._retry_timer = None
        if self._stopping or self.address is not None:
            return
        # HA retains unchanged advertisements. A forgotten address can
        # enter discovery again when it next advertises.
        for address, candidate in tuple(self._candidates.items()):
            if candidate.retry_at is not None and bluetooth.async_last_service_info(
                self.hass, address, connectable=False
            ) is None:
                self._forget_candidate(address)
        self._try_discovery()

    def _try_discovery(self) -> None:
        if self._stopping or self.address is not None:
            return
        self._cancel_retry_timer()
        pending = [
            (candidate.retry_at, address)
            for address, candidate in self._candidates.items()
            if candidate.retry_at is not None
        ]
        if not pending:
            return
        due, address = min(pending, key=lambda entry: entry[0])
        now = time.monotonic()
        busy = self._connecting()
        if due <= now + 0.001 and not busy:
            self._candidates[address].retry_at = None
            self._schedule_connect(address)
            return
        delay = (
            CHARGER_DISCOVERY_RETRY_BUSY_SECONDS
            if busy and due <= now + 0.001
            else max(0.0, due - now)
        )
        self._retry_timer = self.hass.loop.call_later(delay, self._discovery_wakeup)

    def _candidate_is_current(
        self, address: str, candidate: _ChargerCandidate | None, revision: int
    ) -> bool:
        return self._candidates.get(address) is candidate and (
            candidate is None or candidate.pairing_revision == revision
        )

    def _discovery_failed(
        self, address: str, candidate: _ChargerCandidate | None, revision: int
    ) -> None:
        if self._stopping or self.address is not None:
            return
        if candidate is None or not self._candidate_is_current(address, candidate, revision):
            return
        delay = min(
            CHARGER_DISCOVERY_RETRY_MAX_SECONDS,
            CHARGER_DISCOVERY_RETRY_BASE_SECONDS
            * 2 ** candidate.failures
            * random.uniform(0.8, 1.2),
        )
        candidate.failures = min(candidate.failures + 1, 8)
        candidate.retry_at = time.monotonic() + delay

    def _schedule_active_reconnect(self) -> None:
        if (
            self._stopping
            or not self.address
            or not self.available
            or not self._active_retries_remaining
            or self._retry_timer is not None
            or self._owns_client(self._client)
        ):
            return
        self._retry_timer = self.hass.loop.call_later(
            CHARGER_ACTIVE_PROBE_INTERVAL_SECONDS, self._retry_active_connection
        )

    @callback
    def _retry_active_connection(self) -> None:
        self._retry_timer = None
        if (
            self._stopping or not self.address or not self.available
            or self._owns_client(self._client)
        ):
            return
        if not self._connecting() and self._active_retries_remaining:
            self._schedule_connect(self.address)
        else:
            self._schedule_active_reconnect()

    def _schedule_connect(self, address: str) -> None:
        if self._stopping or self._connecting():
            return
        self._connect_task = self.hass.async_create_task(self._async_connect(address))
        self._connect_task.add_done_callback(self._connection_finished)

    def _connection_finished(self, task: asyncio.Task) -> None:
        if task is not self._connect_task or self._stopping:
            return
        if self.address is None:
            self._try_discovery()
        else:
            self._schedule_active_reconnect()

    def _owns_client(self, client: BleakClientWithServiceCache | None) -> bool:
        return not self._stopping and is_current_client(client, self._client)

    async def _async_connect(self, address: str) -> None:
        async with self._connect_lock:
            if self._stopping or self.address not in (None, address):
                return
            if self._client and self._client.is_connected:
                if self.address == address:
                    self._ensure_live_task()
                return
            if self._client is not None:
                self._detach_client()
            await self._client_cleanup.wait()
            if self._stopping or self.address not in (None, address):
                return
            candidate = self._candidates.get(address)
            revision = candidate.pairing_revision if candidate else 0
            if candidate is not None and revision and not candidate.bits & 0x40:
                return
            recovering = self._active_retries_remaining > 0
            if recovering:
                self._active_retries_remaining -= 1
            self._last_probe_monotonic = time.monotonic()
            ble_device = bluetooth.async_ble_device_from_address(
                self.hass, address, connectable=True
            )
            if ble_device is None:
                _LOGGER.debug("iO Sense %s has no connectable path", address)
                self._discovery_failed(address, candidate, revision)
                return

            client = None
            try:
                client = await establish_connection(
                    BleakClientWithServiceCache,
                    ble_device,
                    self.name,
                    disconnected_callback=self._on_disconnect,
                    max_attempts=3,
                )
                if self._stopping or not client.is_connected:
                    self._discovery_failed(address, candidate, revision)
                    self._client_cleanup.start(client)
                    await self._client_cleanup.wait()
                    return
                self._client = client

                def receive_read(char: BleakGATTCharacteristic, payload: bytearray) -> None:
                    if self._owns_client(client):
                        self._on_read(char, payload)

                def receive_status(char: BleakGATTCharacteristic, payload: bytearray) -> None:
                    if self._owns_client(client):
                        self._on_status(char, payload)

                for uuid, receive in (
                    (IOSENSE_READ_UUID, receive_read),
                    (IOSENSE_STATUS_UUID, receive_status),
                ):
                    async with asyncio.timeout(BLE_NOTIFY_TIMEOUT_SECONDS):
                        await client.start_notify(uuid, receive)
                    if not self._owns_client(client):
                        self._discovery_failed(address, candidate, revision)
                        return

                if self.address is None and not await self._async_identify(
                    address, client, candidate, revision
                ):
                    return
                self.data["bridge_connected"] = True
                self.data["state"] = "connected"
                if self.data.get("brush_connected") or recovering:
                    status_received = await self._async_refresh_session_state()
                    if not self._owns_client(client):
                        return
                    if not status_received:
                        brush_active = (
                            self.data["brush_connected"] and not self.data["brush_charging"]
                        )
                        if brush_active and not recovering and self._advertisement_available:
                            self._active_retries_remaining = CHARGER_ACTIVE_RECONNECT_ATTEMPTS
                        if recovering or self._active_retries_remaining:
                            return
                snapshot_due = (
                    not self._snapshot_complete
                    or time.monotonic() - self._last_snapshot_monotonic
                    >= CHARGER_SNAPSHOT_INTERVAL_SECONDS
                )
                if not self._session_running and snapshot_due:
                    await self._async_snapshot()
                if not self._owns_client(client):
                    return
                if self._session_running:
                    self._active_retries_remaining = 0
                    self._cancel_retry_timer()
                    self._ensure_live_task()
                elif self.data.get("brush_connected"):
                    await self._async_post_session_sync()
            except asyncio.CancelledError:
                if client is not None and client is self._client:
                    await self._async_disconnect()
                raise
            except (BleakError, TimeoutError) as err:
                _LOGGER.debug("iO Sense %s connection/setup failed: %s", address, err)
                self._discovery_failed(address, candidate, revision)
                if client is not None and client is self._client:
                    await self._async_disconnect()
            finally:
                if client is not None and client is self._client and (
                    not self._owns_client(client) or not self._session_running
                ):
                    await self._async_disconnect()
                self._push()

    async def _async_identify(
        self,
        address: str,
        client: BleakClientWithServiceCache,
        candidate: _ChargerCandidate | None,
        revision: int,
    ) -> bool:
        if not self._candidate_is_current(address, candidate, revision):
            return False
        identity = await self._async_get(ChargerCommand.BRUSH_DATA)
        if not self._candidate_is_current(address, candidate, revision):
            return False
        if not self._owns_client(client):
            self._discovery_failed(address, candidate, revision)
            return False
        value = identity.value if identity and isinstance(identity.value, dict) else {}
        paired_mac = value.get("brush_mac")
        normalized = normalize_mac(paired_mac) if isinstance(paired_mac, str) else None
        if normalized in (None, "000000000000", "FFFFFFFFFFFF"):
            _LOGGER.debug("iO Sense %s did not report a usable paired brush", address)
            self._discovery_failed(address, candidate, revision)
            return False
        if normalized != normalize_mac(self.parent.address):
            if candidate is not None:
                candidate.retry_at = None
            _LOGGER.debug(
                "iO Sense %s belongs to brush %s, not %s",
                address, paired_mac, self.parent.address,
            )
            return False

        if all(
            value.get(key) is not None
            for key in ("model_id", "protocol_version", "firmware_revision")
        ):
            self.parent._apply_device_identity(
                value["model_id"],
                value["protocol_version"],
                value["firmware_revision"],
                second_controller_version=value.get("second_controller_version"),
                media_content_version=value.get("media_content_version"),
            )
        self.address = address
        self.data["address"] = address
        self._cancel_retry_timer()
        if candidate is not None:
            candidate.stop_tracking()
            candidate.failures = 0
            info = candidate.advertisement
            self._publish_advertisement(
                info,
                decode_charger_advertisement(info.manufacturer_data[ORALB_MANUFACTURER_ID]),
            )
        self._unsub_unavailable = bluetooth.async_track_unavailable(
            self.hass, self._async_unavailable, address, connectable=True
        )
        self.parent._charger_discovered(self)
        async_dispatcher_send(
            self.hass,
            f"{SIGNAL_CHARGER_DISCOVERED}_{self.parent.address}",
        )
        return True

    async def _async_snapshot(self) -> None:
        client = self._client
        successful = 0
        for command in CHARGER_SNAPSHOT_COMMANDS:
            if not self._owns_client(client) or self._session_running:
                return
            if await self._async_get(command) is not None:
                successful += 1
        if self._owns_client(client):
            self._snapshot_complete = successful > len(CHARGER_SNAPSHOT_COMMANDS) // 2
            if successful:
                self._last_snapshot_monotonic = time.monotonic()

    async def _async_refresh_session_state(self) -> bool:
        """Read the two short charger values that gate live forwarding."""
        client = self._client
        # Read the authoritative brush status before the fallback. The charger
        # may still report an earlier session as active when the brush is idle.
        brush_status = await self._async_get(ChargerCommand.BRUSH_STATUS)
        session_status = None
        if self._owns_client(client):
            session_status = await self._async_get(ChargerCommand.SESSION_STATUS)
        return brush_status is not None or session_status is not None

    async def _async_post_session_sync(self, *, force: bool = False) -> None:
        """Collect retained brush data while the charger still owns the slot."""
        if not force and (
            time.monotonic() - self._last_session_sync_monotonic
            < CHARGER_SESSION_SYNC_INTERVAL_SECONDS
        ):
            return
        client = self._client
        generation = self._stream_generation
        successful = 0
        for short_uuid in CHARGER_POST_SESSION_READS:
            if (
                not self._owns_client(client)
                or self._session_running
                or generation != self._stream_generation
            ):
                return
            value = await self._async_passthrough(short_uuid)
            if (
                value
                and self._owns_client(client)
                and generation == self._stream_generation
            ):
                await self.parent._async_apply_charger_passthrough(short_uuid, value)
                successful += 1
        if (
            successful
            and self._owns_client(client)
            and generation == self._stream_generation
        ):
            self._last_session_sync_monotonic = time.monotonic()

    async def _async_get(self, command: ChargerCommand) -> ChargerPacket | None:
        response = await self._async_request(
            command, ((IOSENSE_COMMAND_UUID, build_charger_get(command)),)
        )
        return response if isinstance(response, ChargerPacket) else None

    async def _async_passthrough(self, short_uuid: str) -> bytes | None:
        record = await self._async_request(
            ChargerCommand.BRUSH_PASSTHROUGH,
            (
                (IOSENSE_COMMAND_UUID, bytes((IOSENSE_POST, ChargerCommand.BRUSH_PASSTHROUGH))),
                (IOSENSE_WRITE_UUID, build_passthrough_read(short_uuid)),
            ),
            short_uuid=short_uuid.upper(),
        )
        return record["data"] if isinstance(record, dict) and record["success"] else None

    async def _async_request(
        self,
        command: ChargerCommand,
        writes: tuple[tuple[str, bytes], ...],
        *,
        short_uuid: str | None = None,
    ) -> ChargerPacket | dict[str, Any] | None:
        client = self._client
        if client is None or not self._owns_client(client):
            return None
        async with self._request_lock:
            if not self._owns_client(client):
                return None
            loop = asyncio.get_running_loop()
            future = loop.create_future()
            self._pending_future = future
            self._pending_command = command
            self._pending_short_uuid = short_uuid
            started = loop.time()
            try:
                # The same deadline covers all writes and the response.
                async with asyncio.timeout(CHARGER_BRIDGE_REQUEST_TIMEOUT_SECONDS):
                    for uuid, frame in writes:
                        if not self._owns_client(client):
                            return None
                        await client.write_gatt_char(uuid, frame, response=True)
                    if not self._owns_client(client):
                        return None
                    # The reply can arrive before the delimiter write returns.
                    self._pending_response_armed = True
                    await client.write_gatt_char(
                        IOSENSE_COMMAND_UUID, IOSENSE_PROTOCOL_END, response=True
                    )
                    if not self._owns_client(client):
                        return None
                    response = await future
                if not self._owns_client(client):
                    return None
                self.data["last_response_latency_ms"] = round(
                    (loop.time() - started) * 1000, 1
                )
                return response
            except (BleakError, TimeoutError) as err:
                label = f"passthrough {short_uuid}" if short_uuid else f"GET {command.name}"
                _LOGGER.debug("iO Sense %s failed: %s", label, err)
                return None
            finally:
                self._clear_pending(future)

    def _clear_pending(self, future: asyncio.Future) -> None:
        if self._pending_future is future:
            self._pending_future = None
            self._pending_command = None
            self._pending_short_uuid = None
            self._pending_response_armed = False
        if not future.done():
            future.cancel()

    def _on_read(self, _char: BleakGATTCharacteristic, payload: bytearray) -> None:
        future = self._pending_future
        if not self._pending_response_armed or future is None or future.done():
            return
        packet = decode_charger_read(payload)
        if packet.command is None or packet.command != self._pending_command:
            return
        response: ChargerPacket | dict[str, Any] = packet
        if packet.command == ChargerCommand.BRUSH_PASSTHROUGH:
            if packet.operation != IOSENSE_POST:
                return
            for record in packet.value:
                if (
                    record.get("short_uuid") == self._pending_short_uuid
                    and record.get("operation") == IOSENSE_BRUSH_READ
                ):
                    response = record
                    break
            else:
                return
        elif packet.operation != IOSENSE_GET or (
            not packet.payload
            and packet.command in {ChargerCommand.BRUSH_STATUS, ChargerCommand.SESSION_STATUS}
        ):
            return
        if isinstance(response, ChargerPacket) or (response["success"] and response["data"]):
            self._last_live_response_monotonic = time.monotonic()
        future.set_result(response)
        if self.address is not None and packet.operation == IOSENSE_GET:
            self._apply_native_packet(packet)
        self._push()

    def _on_status(self, _char: BleakGATTCharacteristic, payload: bytearray) -> None:
        # Status packets acknowledge delivery; the read response carries the
        # actual value and is the only packet used to complete a request.
        _LOGGER.debug("iO Sense status: %s", bytes(payload).hex())

    def _apply_native_packet(self, packet: ChargerPacket) -> None:
        if packet.command in _CHARGER_DATA_KEYS:
            self.data[_CHARGER_DATA_KEYS[packet.command]] = packet.value
        if packet.command == ChargerCommand.BRUSH_STATUS:
            self._connection_brush_status = packet.value
            if packet.value == "not_connected":
                self.data["brush_connected"] = False
                self.data["brush_charging"] = False
            elif packet.value == "charging":
                self.data["brush_connected"] = True
                self.data["brush_charging"] = True
            elif packet.value in {"pre_run", "run", "idle"}:
                self.data["brush_connected"] = True
                self.data["brush_charging"] = False
        elif packet.command == ChargerCommand.SESSION_STATUS:
            self._connection_session_status = packet.value
        if packet.command in {
            ChargerCommand.SESSION_STATUS,
            ChargerCommand.BRUSH_STATUS,
        }:
            if packet.value in {"not_connected", "idle", "charging", "run", "pre_run"}:
                self._active_retries_remaining = 0
                self._cancel_retry_timer()
            running = resolve_charger_session_running(
                self._connection_session_status,
                self._connection_brush_status,
                self._session_running,
            )
            if running:
                self._start_session()
            else:
                self._end_session()

    def _start_session(self) -> None:
        """Start or recover a charger-managed brush stream."""
        confirmed = charger_session_confirmed(
            self._connection_session_status, self._connection_brush_status
        )
        self._cancel_disconnect()
        if not self._session_running:
            self._stream_generation += 1
            self._session_running = True
            self._session_confirmed = False
            self._provisional_timer_baseline = None
        if confirmed and not self._session_confirmed:
            self._confirm_session()
        elif self._session_confirmed and not self.parent._session_active:
            # Re-enter tracking after a reload or missed state edge while the
            # charger still reports an active brush state.
            self.parent._charger_session_started(confirmed=True)
        self._ensure_live_task()

    def _confirm_session(self) -> None:
        """Promote provisional charger ownership to a real brush session."""
        if not self._session_running or self._session_confirmed:
            return
        self._session_confirmed = True
        self._provisional_timer_baseline = None
        self.parent._charger_session_started(confirmed=True)

    def _observe_provisional_passthrough(
        self, short_uuid: str, payload: bytes | bytearray
    ) -> bool:
        """Use authoritative brush values to confirm a provisional pre_run."""
        raw = bytes(payload)
        if self._session_confirmed:
            return True
        if short_uuid == "FF04" and raw and raw[0] == RUNNING_STATE:
            self._confirm_session()
            return True
        if short_uuid != "FF08" or len(raw) < 2:
            return False
        seconds = raw[0] * 60 + raw[1]
        baseline = self._provisional_timer_baseline
        if baseline is None or seconds < baseline:
            self._provisional_timer_baseline = seconds
            return False
        if seconds <= baseline:
            return False
        self._confirm_session()
        return True

    def _end_session(self, *, schedule_disconnect: bool = True) -> None:
        """Finish a charger-managed stream once the brush is genuinely quiet."""
        if not self._session_running:
            return
        confirmed = self._session_confirmed
        self._session_running = False
        self._session_confirmed = False
        self._provisional_timer_baseline = None
        self._cancel_live_task()
        if confirmed:
            self.parent._charger_session_ended()
        if schedule_disconnect:
            self._schedule_disconnect()

    def _ensure_live_task(self) -> None:
        if not self._session_running:
            return
        if self._live_task and not self._live_task.done():
            return
        self._live_task = self.hass.async_create_task(self._async_live_loop())

    def _cancel_live_task(self) -> None:
        task, self._live_task = self._live_task, None
        if task is not None and not task.done() and task is not asyncio.current_task():
            task.cancel()

    async def _async_live_loop(self) -> None:
        client = self._client
        generation = self._stream_generation

        def owns_stream() -> bool:
            return (
                self._owns_client(client)
                and self._session_running
                and generation == self._stream_generation
            )

        tick = 0
        mode_observed = False
        continuation_reads = 0
        stream_started = time.monotonic()
        try:
            while owns_stream():
                started = time.monotonic()
                reading_continuation = False
                if not self._session_confirmed:
                    # pre_run is also emitted in the handle settings menu.
                    if tick > 0 and tick % CHARGER_BRUSH_STATUS_EVERY_TICKS == 0:
                        await self._async_get(ChargerCommand.BRUSH_STATUS)
                    else:
                        action = "FF04" if tick % 2 == 0 else "FF08"
                        value = await self._async_passthrough(action)
                        if value and owns_stream() and self._observe_provisional_passthrough(
                            action, value
                        ):
                            await self.parent._async_apply_charger_passthrough(action, value)
                else:
                    pressure = await self._async_passthrough("FF0B")
                    if not owns_stream():
                        return
                    if pressure:
                        await self.parent._async_apply_charger_passthrough("FF0B", pressure)
                    if not owns_stream():
                        return

                    # On a slow reconnect, waiting through the entire startup
                    # cadence can outlast the pause grace. Give the tracker
                    # two early raw timer samples and room for one lost reply.
                    # Keep two reads per tick and resume the normal startup
                    # sequence once the timer evidence resolves the boundary.
                    reading_continuation = (
                        continuation_reads < CHARGER_CONTINUATION_TIMER_ATTEMPTS
                        and self.parent.charger_continuation_pending
                    )
                    if reading_continuation:
                        action = "FF08"
                        continuation_reads += 1
                    else:
                        action = charger_live_auxiliary(
                            tick,
                            mode_observed=mode_observed,
                            brush_status_every_ticks=CHARGER_BRUSH_STATUS_EVERY_TICKS,
                            battery_every_ticks=CHARGER_BATTERY_EVERY_TICKS,
                        )
                    if action == "BRUSH_STATUS":
                        await self._async_get(ChargerCommand.BRUSH_STATUS)
                    else:
                        value = await self._async_passthrough(action)
                        if value and owns_stream():
                            await self.parent._async_apply_charger_passthrough(action, value)
                            if action == "FF07":
                                mode_observed = True
                    if not owns_stream():
                        return
                    self.parent._advance_charger_timer()
                    self.parent.data["data_source"] = DATA_SOURCE_CHARGER
                    self.parent.data["live"] = True
                    self.parent._push()
                if not owns_stream():
                    return
                last_response = max(stream_started, self._last_live_response_monotonic)
                if time.monotonic() - last_response >= CHARGER_LIVE_RESPONSE_TIMEOUT_SECONDS:
                    _LOGGER.debug("iO Sense %s stopped replying during live reads", self.address)
                    self._handle_client_disconnect(client)
                    return
                self._push()
                if not reading_continuation:
                    tick += 1
                remaining = CHARGER_BRIDGE_INTERVAL_SECONDS - (time.monotonic() - started)
                if remaining > 0:
                    await asyncio.sleep(remaining)
        except asyncio.CancelledError:
            pass
        finally:
            if client is self._client and generation == self._stream_generation:
                self.parent.data["live"] = False
                self.parent._push()

    def _schedule_disconnect(self) -> None:
        if self._disconnect_task and not self._disconnect_task.done():
            return

        client = self._client
        generation = self._stream_generation

        async def _release() -> None:
            if (
                self._owns_client(client)
                and not self._session_running
                and generation == self._stream_generation
            ):
                # At active_idle the private charger-to-brush connection can
                # still exist for only a brief window. Read FF05 first (via
                # CHARGER_POST_SESSION_READS) before waiting to disconnect;
                # delaying the read made the battery remain at its pre-session
                # value on chargers that release the brush quickly.
                await self._async_post_session_sync(force=True)
            await asyncio.sleep(CHARGER_IDLE_DISCONNECT_SECONDS)
            if (
                self._owns_client(client)
                and not self._session_running
                and generation == self._stream_generation
            ):
                await self._async_disconnect()

        self._disconnect_task = self.hass.async_create_task(_release())

    def _cancel_disconnect(self) -> None:
        task, self._disconnect_task = self._disconnect_task, None
        if task is not None and not task.done() and task is not asyncio.current_task():
            task.cancel()

    def _on_disconnect(self, client: BleakClientWithServiceCache) -> None:
        self.hass.loop.call_soon_threadsafe(self._handle_client_disconnect, client)

    @callback
    def _handle_client_disconnect(self, client: BleakClientWithServiceCache) -> None:
        if client is not self._client:
            return
        was_running = self._session_running
        self._detach_client()
        if was_running and not self._stopping and self.address and self.available:
            self._active_retries_remaining = CHARGER_ACTIVE_RECONNECT_ATTEMPTS
            self._schedule_active_reconnect()

    def _detach_client(self) -> None:
        """Invalidate connection-owned work before releasing its BLE resources."""
        client, self._client = self._client, None
        self.available = self._advertisement_available
        self._cancel_disconnect()
        self._end_session(schedule_disconnect=False)
        self._cancel_live_task()
        if (future := self._pending_future) is not None:
            if not future.done():
                future.set_result(None)
            self._clear_pending(future)
        self._last_live_response_monotonic = 0.0
        self.data.update(
            bridge_connected=False,
            state="available" if self.available else "unavailable",
        )
        # Keep the last sampled diagnostic values visible. Only the current
        # connection's samples can decide whether brushing is active.
        self._connection_brush_status = None
        self._connection_session_status = None
        self.parent.data["live"] = False
        if client is not None:
            self._client_cleanup.start(client)
        self._push()
        self.parent._push()

    async def _async_disconnect(self) -> None:
        self._detach_client()
        await self._client_cleanup.wait()

    def _push(self) -> None:
        async_dispatcher_send(
            self.hass,
            f"{SIGNAL_CHARGER_UPDATE}_{self.parent.address}",
            self.data,
        )
