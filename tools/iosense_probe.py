#!/usr/bin/env python3
"""Capture read-only diagnostics from an Oral-B iO Sense or toothbrush.

The default command connects to the selected charger, enumerates GATT, reads
characteristics marked readable, attempts read-only descriptor reads, and—if
the known iO Sense command transport exists—sends a small set of GET requests.
It never sends POST/SET commands or writes a command payload.

With ``--brush-pacer``, it instead reads the toothbrush's relevant
characteristics and records raw brush-state, mode, timer, and sector
notifications for one brushing session. It does not write characteristic
values or change persistent toothbrush settings. Enabling notifications may
cause the Bluetooth stack to write temporary Client Characteristic
Configuration descriptors; those subscriptions are removed before exit.
"""

from __future__ import annotations

import argparse
import asyncio
from dataclasses import dataclass
from datetime import datetime, timezone
import importlib.metadata
import json
from pathlib import Path
import platform
import sys
from typing import Any, Iterable


TOOL_VERSION = "2"
ORALB_MANUFACTURER_ID = 220
IOSENSE_SERVICE_UUID = "a0f03e00-5047-4d53-8208-4f72616c2d42"
IOSENSE_COMMAND_UUID = "a0f03c00-5047-4d53-8208-4f72616c2d42"
IOSENSE_READ_UUID = "a0f03c01-5047-4d53-8208-4f72616c2d42"
IOSENSE_WRITE_UUID = "a0f03c02-5047-4d53-8208-4f72616c2d42"
IOSENSE_STATUS_UUID = "a0f03c03-5047-4d53-8208-4f72616c2d42"
IOSENSE_PROTOCOL_END = b"\xe0"
GET_OPERATION = 0xC0

BRUSH_DEVICE_INFO_UUID = "a0f0ff02-5047-4d53-8208-4f72616c2d42"
BRUSH_STATE_UUID = "a0f0ff04-5047-4d53-8208-4f72616c2d42"
BRUSH_MODE_UUID = "a0f0ff07-5047-4d53-8208-4f72616c2d42"
BRUSH_TIME_UUID = "a0f0ff08-5047-4d53-8208-4f72616c2d42"
BRUSH_SECTOR_UUID = "a0f0ff09-5047-4d53-8208-4f72616c2d42"
BRUSH_AVAILABLE_MODES_UUID = "a0f0ff25-5047-4d53-8208-4f72616c2d42"
BRUSH_PACER_UUID = "a0f0ff26-5047-4d53-8208-4f72616c2d42"
BRUSH_RUNNING_STATE = 3
BRUSH_NOTIFY_CHARACTERISTICS: tuple[tuple[str, str], ...] = (
    ("state", BRUSH_STATE_UUID),
    ("mode", BRUSH_MODE_UUID),
    ("timer", BRUSH_TIME_UUID),
    ("sector", BRUSH_SECTOR_UUID),
)
BRUSH_INITIAL_READS: tuple[tuple[str, str], ...] = (
    ("device_info_ff02", BRUSH_DEVICE_INFO_UUID),
    ("available_modes_ff25", BRUSH_AVAILABLE_MODES_UUID),
    ("pacer_configuration_ff26", BRUSH_PACER_UUID),
    ("sector_ff09", BRUSH_SECTOR_UUID),
)
BRUSH_FINAL_READS: tuple[tuple[str, str], ...] = (
    ("available_modes_ff25", BRUSH_AVAILABLE_MODES_UUID),
    ("pacer_configuration_ff26", BRUSH_PACER_UUID),
    ("sector_ff09", BRUSH_SECTOR_UUID),
)

SERVER_MODES = {0: "full", 1: "limited", 2: "wifi_only", 3: "aws_only"}

# Deliberately small and read-only. These commands expose identity and protocol
# compatibility without walking configuration, credentials, update, reset, or
# other state-changing areas of the charger protocol.
READ_ONLY_PROBES: tuple[tuple[str, int], ...] = (
    ("firmware", 0x1E),
    ("hardware_version", 0x25),
    ("server_mode", 0x26),
    ("device_id", 0x03),
    ("brush_data", 0x39),
)


def utc_now() -> str:
    """Return an ISO UTC timestamp."""
    return datetime.now(timezone.utc).isoformat()


def error_text(error: BaseException) -> str:
    """Format exceptions usefully even when their message is empty."""
    message = str(error).strip()
    return f"{type(error).__name__}: {message}" if message else type(error).__name__


def normalize_uuid(value: str) -> str:
    """Normalize UUID text for portable comparisons."""
    return value.lower()


def decode_legacy_advertisement(payload: bytes) -> dict[str, Any]:
    """Decode the known 14-byte Stargate advertisement without rejecting it."""
    result: dict[str, Any] = {"length": len(payload), "hex": payload.hex()}
    if len(payload) != 14:
        result["compatible"] = False
        result["reason"] = f"expected 14 bytes, got {len(payload)}"
        if len(payload) > 1:
            result["protocol_version"] = payload[0]
            result["device_type_byte"] = payload[1]
        return result

    status = payload[13]
    result.update(
        {
            "compatible": payload[1] == 0xA2,
            "protocol_version": payload[0],
            "device_type_byte": payload[1],
            "device_type": "stargate" if payload[1] == 0xA2 else "unknown",
            "firmware": f"{payload[2]}.{payload[3]}.{payload[4]}",
            "server_mode": SERVER_MODES.get(payload[5], f"mode_{payload[5]}"),
            "advertised_mac": ":".join(f"{part:02X}" for part in payload[6:12]),
            "body_color": {0: "white", 1: "black"}.get(
                payload[12], f"color_{payload[12]}"
            ),
            "status": {
                "raw": status,
                "wifi_connected": bool(status & 0x01),
                "internet_connected": bool(status & 0x02),
                "cloud_connected": bool(status & 0x04),
                "brush_connected": bool(status & 0x08),
                "brush_charging": bool(status & 0x10),
                "touchpad_active": bool(status & 0x20),
                "brush_paired": bool(status & 0x40),
                "demo_mode": bool(status & 0x80),
            },
        }
    )
    if payload[1] != 0xA2:
        result["reason"] = f"expected device type 0xa2, got 0x{payload[1]:02x}"
    return result


@dataclass(frozen=True)
class Candidate:
    """One BLE device and its most recent advertisement."""

    device: Any
    advertisement: Any
    score: int
    reasons: tuple[str, ...]

    @property
    def address(self) -> str:
        return str(self.device.address)

    @property
    def name(self) -> str:
        return str(
            getattr(self.advertisement, "local_name", None)
            or getattr(self.device, "name", None)
            or "Unknown"
        )

    @property
    def rssi(self) -> int:
        return int(getattr(self.advertisement, "rssi", -127))


def candidate_score(
    name: str,
    service_uuids: Iterable[str],
    manufacturer_data: dict[int, bytes],
    rssi: int,
) -> tuple[int, tuple[str, ...]]:
    """Score a device while keeping every reason visible in the report."""
    normalized_name = name.casefold()
    normalized_uuids = {normalize_uuid(value) for value in service_uuids}
    reasons: list[str] = []
    score = 0

    if IOSENSE_SERVICE_UUID in normalized_uuids:
        score += 100
        reasons.append("known_iosense_service_uuid")
    if "io sense" in normalized_name or "iosense" in normalized_name:
        score += 90
        reasons.append("iosense_local_name")

    oralb_payload = manufacturer_data.get(ORALB_MANUFACTURER_ID)
    if oralb_payload is not None:
        reasons.append("oralb_manufacturer_id_220")
        if len(oralb_payload) == 11:
            # This is the normal toothbrush advertisement, not the charger.
            score -= 100
            reasons.append("toothbrush_11_byte_payload")
        elif len(oralb_payload) == 14 and oralb_payload[1:2] == b"\xa2":
            score += 80
            reasons.append("legacy_stargate_payload")
        else:
            score += 50
            reasons.append(f"unknown_oralb_payload_{len(oralb_payload)}_bytes")

    # RSSI is only a tie-breaker; there is deliberately no signal cutoff.
    if -127 < rssi <= 20:
        score += max(0, min(10, (100 + rssi) // 5))
    return score, tuple(reasons)


def build_candidates(discovered: dict[str, tuple[Any, Any]]) -> list[Candidate]:
    """Build strongest-first candidates from a Bleak discovery result."""
    candidates: list[Candidate] = []
    for device, advertisement in discovered.values():
        name = str(
            getattr(advertisement, "local_name", None)
            or getattr(device, "name", None)
            or ""
        )
        score, reasons = candidate_score(
            name,
            getattr(advertisement, "service_uuids", ()) or (),
            dict(getattr(advertisement, "manufacturer_data", {}) or {}),
            int(getattr(advertisement, "rssi", -127)),
        )
        candidates.append(Candidate(device, advertisement, score, reasons))
    return sorted(candidates, key=lambda item: (item.score, item.rssi), reverse=True)


def is_toothbrush_candidate(candidate: Candidate) -> bool:
    """Recognize the normal Oral-B toothbrush advertisement conservatively."""
    manufacturer_data = dict(
        getattr(candidate.advertisement, "manufacturer_data", {}) or {}
    )
    payload = manufacturer_data.get(ORALB_MANUFACTURER_ID)
    name = candidate.name.casefold()
    name_is_brush = "oral-b" in name and (
        "toothbrush" in name or ("io" in name and "sense" not in name)
    )
    return (payload is not None and len(bytes(payload)) == 11) or name_is_brush


def advertisement_report(candidate: Candidate) -> dict[str, Any]:
    """Serialize the portable advertisement fields Bleak exposes."""
    advertisement = candidate.advertisement
    manufacturer_data = {
        str(company_id): {
            "length": len(bytes(payload)),
            "hex": bytes(payload).hex(),
        }
        for company_id, payload in dict(
            getattr(advertisement, "manufacturer_data", {}) or {}
        ).items()
    }
    service_data = {
        normalize_uuid(uuid): {
            "length": len(bytes(payload)),
            "hex": bytes(payload).hex(),
        }
        for uuid, payload in dict(
            getattr(advertisement, "service_data", {}) or {}
        ).items()
    }
    report: dict[str, Any] = {
        "address": candidate.address,
        "name": candidate.name,
        "rssi": candidate.rssi,
        "tx_power": getattr(advertisement, "tx_power", None),
        "service_uuids": sorted(
            normalize_uuid(value)
            for value in (getattr(advertisement, "service_uuids", ()) or ())
        ),
        "manufacturer_data": manufacturer_data,
        "service_data": service_data,
        "candidate_score": candidate.score,
        "candidate_reasons": list(candidate.reasons),
    }
    oralb_payload = dict(
        getattr(advertisement, "manufacturer_data", {}) or {}
    ).get(ORALB_MANUFACTURER_ID)
    if oralb_payload is not None:
        report["oralb_advertisement"] = decode_legacy_advertisement(
            bytes(oralb_payload)
        )
    return report


def decode_probe_value(command: int, payload: bytes) -> Any:
    """Decode the small read-only identity probe set."""
    if command == 0x1E and len(payload) >= 3:
        return ".".join(str(part) for part in payload[:3])
    if command == 0x25 and payload:
        return payload[0]
    if command == 0x26 and payload:
        return SERVER_MODES.get(payload[0], f"mode_{payload[0]}")
    if command == 0x03:
        return payload.decode("utf-8", errors="replace").replace("\x00", "")
    if command == 0x39:
        value: dict[str, Any] = {"length": len(payload), "hex": payload.hex()}
        if len(payload) >= 7:
            value["brush_mac"] = ":".join(f"{part:02X}" for part in payload[1:7])
        if len(payload) > 40:
            value.update(
                {
                    "model_id": payload[16],
                    "protocol_version": payload[39],
                    "firmware_revision": payload[40],
                }
            )
        return value
    return payload.hex()


class ReadOnlyProtocolProbe:
    """Issue only protocol-v2 GET requests over the known charger transport."""

    def __init__(
        self, client: Any, *, request_timeout: float, frame_delay: float
    ) -> None:
        self.client = client
        self.request_timeout = request_timeout
        self.frame_delay = frame_delay
        self.pending_command: int | None = None
        self.pending_future: asyncio.Future[bytes] | None = None
        self.status_notifications: list[str] = []
        self.frames_written: list[dict[str, str]] = []

    async def start(self) -> None:
        await self.client.start_notify(IOSENSE_READ_UUID, self._on_read)
        await self.client.start_notify(IOSENSE_STATUS_UUID, self._on_status)

    async def stop(self) -> None:
        for uuid in (IOSENSE_STATUS_UUID, IOSENSE_READ_UUID):
            try:
                await self.client.stop_notify(uuid)
            except Exception:
                pass

    def _on_read(self, _characteristic: Any, value: bytearray) -> None:
        raw = bytes(value)
        future = self.pending_future
        if (
            future is not None
            and not future.done()
            and len(raw) >= 2
            and raw[0] == self.pending_command
            and raw[1] == GET_OPERATION
        ):
            future.set_result(raw)

    def _on_status(self, _characteristic: Any, value: bytearray) -> None:
        self.status_notifications.append(bytes(value).hex())

    async def get(self, command: int) -> dict[str, Any]:
        loop = asyncio.get_running_loop()
        future: asyncio.Future[bytes] = loop.create_future()
        self.pending_command = command
        self.pending_future = future
        header = bytes((GET_OPERATION, command))
        try:
            self.frames_written.append(
                {"characteristic": IOSENSE_COMMAND_UUID, "hex": header.hex()}
            )
            await self.client.write_gatt_char(
                IOSENSE_COMMAND_UUID, header, response=True
            )
            if self.frame_delay:
                await asyncio.sleep(self.frame_delay)
            self.frames_written.append(
                {
                    "characteristic": IOSENSE_COMMAND_UUID,
                    "hex": IOSENSE_PROTOCOL_END.hex(),
                }
            )
            await self.client.write_gatt_char(
                IOSENSE_COMMAND_UUID, IOSENSE_PROTOCOL_END, response=True
            )
            raw = await asyncio.wait_for(future, timeout=self.request_timeout)
            payload = raw[2:]
            return {
                "success": True,
                "raw": raw.hex(),
                "payload": payload.hex(),
                "value": decode_probe_value(command, payload),
            }
        except Exception as error:
            return {"success": False, "error": error_text(error)}
        finally:
            self.pending_command = None
            self.pending_future = None
            if not future.done():
                future.cancel()


async def read_characteristic(
    client: Any, uuid: str, *, read_timeout: float
) -> dict[str, Any]:
    """Read one characteristic while preserving the exact returned bytes."""
    try:
        value = bytes(
            await asyncio.wait_for(
                client.read_gatt_char(uuid), timeout=read_timeout
            )
        )
        return {"success": True, "length": len(value), "hex": value.hex()}
    except Exception as error:
        return {"success": False, "error": error_text(error)}


def annotate_brush_read(name: str, result: dict[str, Any]) -> None:
    """Add non-authoritative positional hints without replacing raw evidence."""
    if not result.get("success"):
        return
    raw = bytes.fromhex(result["hex"])
    if name == "device_info_ff02" and len(raw) >= 3:
        result["positional_hints"] = {
            "model_id_raw": raw[0],
            "protocol_version_raw": raw[1],
            "firmware_revision_raw": raw[2],
        }
    elif name == "available_modes_ff25":
        result["positional_hints"] = {
            "mode_values_raw": list(raw),
            "mode_value_count": len(raw),
        }
    elif name == "pacer_configuration_ff26":
        usable = [value for value in raw if 0 < value < 0xFF]
        result["positional_hints"] = {
            "byte_values": list(raw),
            "usable_sector_seconds": usable,
            "usable_sector_count": len(usable),
            "target_duration_seconds": sum(usable),
        }
    elif name == "sector_ff09" and raw:
        hints: dict[str, int] = {"sector_raw": raw[0]}
        if len(raw) >= 2:
            hints["sector_timer_raw"] = raw[1]
        if len(raw) >= 3:
            hints["total_hint_raw"] = raw[2]
        result["positional_hints"] = hints


async def read_brush_snapshot(
    client: Any,
    characteristics: tuple[tuple[str, str], ...],
    *,
    read_timeout: float,
) -> dict[str, dict[str, Any]]:
    """Read and annotate one exact brush configuration snapshot."""
    snapshot: dict[str, dict[str, Any]] = {}
    for name, uuid in characteristics:
        result = await read_characteristic(client, uuid, read_timeout=read_timeout)
        annotate_brush_read(name, result)
        snapshot[name] = result
    return snapshot


class BrushPacerCapture:
    """Record one toothbrush session without writing characteristic values."""

    def __init__(self, client: Any) -> None:
        self.client = client
        self.loop = asyncio.get_running_loop()
        self.started_at = self.loop.time()
        self.subscribed: list[str] = []
        self.subscription_errors: dict[str, str] = {}
        self.unsubscribe_errors: dict[str, str] = {}
        self.notifications: list[dict[str, Any]] = []
        self.running_seen = False
        self.end_state_raw: int | None = None
        self.finished = asyncio.Event()

    async def start(self) -> None:
        """Subscribe to all useful session characteristics, including FF09."""
        for name, uuid in BRUSH_NOTIFY_CHARACTERISTICS:
            try:
                await self.client.start_notify(uuid, self._callback(name, uuid))
                self.subscribed.append(uuid)
            except Exception as error:
                self.subscription_errors[name] = error_text(error)
        required = (
            ("FF04 state", "state", BRUSH_STATE_UUID),
            ("FF09 sector", "sector", BRUSH_SECTOR_UUID),
        )
        missing = [
            f"{label}: {self.subscription_errors.get(name, 'unknown error')}"
            for label, name, uuid in required
            if uuid not in self.subscribed
        ]
        if missing:
            raise RuntimeError(
                "required notification subscription(s) failed: " + "; ".join(missing)
            )

    async def stop(self) -> None:
        """Remove every notification subscription that was successfully added."""
        names = {uuid: name for name, uuid in BRUSH_NOTIFY_CHARACTERISTICS}
        for uuid in reversed(self.subscribed):
            try:
                await self.client.stop_notify(uuid)
            except Exception as error:
                self.unsubscribe_errors[names[uuid]] = error_text(error)

    def _callback(self, name: str, uuid: str) -> Any:
        def record(_characteristic: Any, value: bytearray) -> None:
            raw = bytes(value)
            item: dict[str, Any] = {
                "captured_at": utc_now(),
                "elapsed_seconds": round(self.loop.time() - self.started_at, 3),
                "characteristic": name,
                "uuid": uuid,
                "length": len(raw),
                "hex": raw.hex(),
            }
            # These fields are positional annotations only. The raw value above
            # remains the evidence and does not depend on integration decoders.
            if name == "state" and raw:
                item["state_raw"] = raw[0]
                if raw[0] == BRUSH_RUNNING_STATE:
                    self.running_seen = True
                elif self.running_seen and not self.finished.is_set():
                    self.end_state_raw = raw[0]
                    self.finished.set()
            elif name == "mode" and raw:
                item["mode_raw"] = raw[0]
            elif name == "timer" and len(raw) >= 2:
                item["minutes_raw"] = raw[0]
                item["seconds_raw"] = raw[1]
                item["brushing_time_seconds_hint"] = raw[0] * 60 + raw[1]
            elif name == "sector":
                if len(raw) >= 1:
                    item["sector_raw"] = raw[0]
                if len(raw) >= 2:
                    item["sector_timer_raw"] = raw[1]
                if len(raw) >= 3:
                    item["total_hint_raw"] = raw[2]
            self.notifications.append(item)

        return record

    async def wait(self, *, session_timeout: float, end_grace: float = 2.0) -> str:
        """Wait for running-to-not-running, retaining a short notification tail."""
        try:
            await asyncio.wait_for(self.finished.wait(), timeout=session_timeout)
        except asyncio.TimeoutError:
            return "timeout"
        if end_grace:
            await asyncio.sleep(end_grace)
        return "state_after_running"

    def report(self, ended_reason: str) -> dict[str, Any]:
        """Return the complete raw capture and minimal session metadata."""
        return {
            "running_state_raw": BRUSH_RUNNING_STATE,
            "running_seen": self.running_seen,
            "ended_reason": ended_reason,
            "end_state_raw": self.end_state_raw,
            "subscribed_characteristics": list(self.subscribed),
            "subscription_errors": dict(self.subscription_errors),
            "unsubscribe_errors": dict(self.unsubscribe_errors),
            "notifications": list(self.notifications),
        }


def characteristic_report(characteristic: Any) -> dict[str, Any]:
    """Return static GATT characteristic metadata."""
    return {
        "uuid": normalize_uuid(str(characteristic.uuid)),
        "handle": getattr(characteristic, "handle", None),
        "description": getattr(characteristic, "description", None),
        "properties": sorted(str(value) for value in characteristic.properties),
        "descriptors": [],
    }


async def capture_gatt(
    client: Any, *, read_timeout: float
) -> tuple[list[dict[str, Any]], set[str]]:
    """Enumerate GATT, reading only readable characteristics and descriptors."""
    services_report: list[dict[str, Any]] = []
    characteristic_uuids: set[str] = set()
    for service in client.services:
        service_value: dict[str, Any] = {
            "uuid": normalize_uuid(str(service.uuid)),
            "handle": getattr(service, "handle", None),
            "description": getattr(service, "description", None),
            "characteristics": [],
        }
        for characteristic in service.characteristics:
            item = characteristic_report(characteristic)
            characteristic_uuids.add(item["uuid"])
            if "read" in item["properties"]:
                try:
                    value = await asyncio.wait_for(
                        client.read_gatt_char(characteristic), timeout=read_timeout
                    )
                    item["read"] = {"success": True, "hex": bytes(value).hex()}
                except Exception as error:
                    item["read"] = {
                        "success": False,
                        "error": error_text(error),
                    }
            for descriptor in characteristic.descriptors:
                descriptor_value: dict[str, Any] = {
                    "uuid": normalize_uuid(str(descriptor.uuid)),
                    "handle": getattr(descriptor, "handle", None),
                    "description": getattr(descriptor, "description", None),
                }
                try:
                    value = await asyncio.wait_for(
                        client.read_gatt_descriptor(descriptor.handle),
                        timeout=read_timeout,
                    )
                    descriptor_value["read"] = {
                        "success": True,
                        "hex": bytes(value).hex(),
                    }
                except Exception as error:
                    descriptor_value["read"] = {
                        "success": False,
                        "error": error_text(error),
                    }
                item["descriptors"].append(descriptor_value)
            service_value["characteristics"].append(item)
        services_report.append(service_value)
    return services_report, characteristic_uuids


def known_transport_present(characteristic_uuids: set[str]) -> bool:
    """Return whether all characteristics required for read-only GET exist."""
    return {
        IOSENSE_COMMAND_UUID,
        IOSENSE_READ_UUID,
        IOSENSE_STATUS_UUID,
    }.issubset(characteristic_uuids)


def choose_candidate(
    candidates: list[Candidate], address: str | None, *, interactive: bool
) -> Candidate:
    """Automatically choose a charger or ask once when discovery is ambiguous."""
    if address:
        wanted = address.casefold()
        for candidate in candidates:
            if candidate.address.casefold() == wanted:
                return candidate
        raise RuntimeError(f"Bluetooth address {address!r} was not found")

    likely = [candidate for candidate in candidates if candidate.score >= 40]
    if len(likely) == 1:
        return likely[0]
    choices = likely or candidates[:10]
    if not choices:
        raise RuntimeError("no Bluetooth Low Energy devices were found")
    if not interactive:
        raise RuntimeError(
            "automatic selection was ambiguous; run interactively or pass --address"
        )

    if likely:
        print("Multiple possible iO Sense devices were found:")
    else:
        print("No known iO Sense signature was found. Select the likely nearby device:")
    for index, candidate in enumerate(choices, start=1):
        manufacturer_ids = sorted(
            dict(getattr(candidate.advertisement, "manufacturer_data", {}) or {})
        )
        print(
            f"  [{index}] {candidate.name}  {candidate.address}  "
            f"RSSI {candidate.rssi}  manufacturer IDs {manufacturer_ids or '-'}"
        )
    while True:
        answer = input("Select device [1]: ").strip() or "1"
        try:
            return choices[int(answer) - 1]
        except (ValueError, IndexError):
            print(f"Enter a number from 1 to {len(choices)}.")


def choose_toothbrush_candidate(
    candidates: list[Candidate], address: str | None, *, interactive: bool
) -> Candidate:
    """Choose only devices that look like toothbrushes unless addressed exactly."""
    if address:
        return choose_candidate(candidates, address, interactive=interactive)

    recognized = [
        candidate for candidate in candidates if is_toothbrush_candidate(candidate)
    ]
    if len(recognized) == 1:
        return recognized[0]
    if not candidates:
        raise RuntimeError("no Bluetooth Low Energy devices were found")
    if not recognized and not interactive:
        raise RuntimeError(
            "no Oral-B toothbrush advertisement was found; wake the brush, "
            "move it nearby, and retry or pass --address"
        )
    if recognized and not interactive:
        raise RuntimeError(
            "multiple toothbrushes were found; run interactively or pass --address"
        )

    choices = recognized or candidates[:10]
    if recognized:
        print("Multiple possible Oral-B toothbrushes were found:")
    else:
        print(
            "No known toothbrush signature was found. Wake the brush, then select "
            "the likely nearby device:"
        )
    for index, candidate in enumerate(choices, start=1):
        print(
            f"  [{index}] {candidate.name}  {candidate.address}  "
            f"RSSI {candidate.rssi}"
        )
    while True:
        answer = input("Select toothbrush [1]: ").strip() or "1"
        try:
            return choices[int(answer) - 1]
        except (ValueError, IndexError):
            print(f"Enter a number from 1 to {len(choices)}.")


def default_output_path(*, brush_pacer: bool = False) -> Path:
    """Return a timestamped report path."""
    timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    prefix = "oralb-brush-pacer" if brush_pacer else "iosense-probe"
    return Path(f"{prefix}-{timestamp}.json")


def format_brush_read(value: dict[str, Any]) -> str:
    """Format one exact read without confusing empty data with no capture."""
    if not value:
        return "not captured"
    if not value.get("success"):
        return f"failed ({value.get('error', 'unknown error')})"
    return f"{value.get('length', 0)} bytes, hex={value.get('hex', '') or '<empty>'}"


def brush_console_summary(report: dict[str, Any]) -> str:
    """Build a compact, privacy-conscious summary for issue comments."""
    advertisement = report["advertisement"]
    brush = report.get("brush_pacer", {})
    initial_reads = brush.get("initial_reads", {})
    final_reads = brush.get("final_reads", {})
    session = brush.get("session", {})
    sector_changes: list[str] = []
    for item in session.get("notifications", []):
        if item.get("characteristic") != "sector":
            continue
        value = item["hex"]
        if not sector_changes or sector_changes[-1] != value:
            sector_changes.append(value)
    lines = [
        "Oral-B brush pacer capture summary",
        f"  Tool version: {report.get('tool_version', 'unknown')}",
        f"  Name: {advertisement['name']}",
        f"  RSSI: {advertisement['rssi']} dBm",
        "  FF02 initial: "
        + format_brush_read(initial_reads.get("device_info_ff02", {})),
        "  FF25 initial -> final: "
        + format_brush_read(initial_reads.get("available_modes_ff25", {}))
        + " -> "
        + format_brush_read(final_reads.get("available_modes_ff25", {})),
        "  FF26 initial -> final: "
        + format_brush_read(initial_reads.get("pacer_configuration_ff26", {}))
        + " -> "
        + format_brush_read(final_reads.get("pacer_configuration_ff26", {})),
        "  FF09 initial -> final: "
        + format_brush_read(initial_reads.get("sector_ff09", {}))
        + " -> "
        + format_brush_read(final_reads.get("sector_ff09", {})),
        f"  Running state observed: {session.get('running_seen', False)}",
        f"  End reason: {session.get('ended_reason', 'not started')}",
        f"  Notifications captured: {len(session.get('notifications', []))}",
        "  FF09 raw changes: " + (", ".join(sector_changes) or "none"),
    ]
    if brush.get("error"):
        lines.append(f"  Capture error: {brush['error']}")
    return "\n".join(lines)


def console_summary(report: dict[str, Any]) -> str:
    """Build a compact issue-friendly summary."""
    if report.get("mode") == "brush_pacer":
        return brush_console_summary(report)
    advertisement = report["advertisement"]
    oralb = advertisement.get("oralb_advertisement")
    lines = [
        "iO Sense probe summary",
        f"  Name: {advertisement['name']}",
        f"  Address: {advertisement['address']}",
        f"  RSSI: {advertisement['rssi']} dBm",
        "  Candidate reasons: "
        + (", ".join(advertisement["candidate_reasons"]) or "none"),
    ]
    if oralb:
        lines.extend(
            [
                f"  Oral-B payload: {oralb['length']} bytes",
                f"  Legacy advertisement compatible: {oralb['compatible']}",
            ]
        )
    gatt = report.get("gatt")
    if gatt:
        lines.extend(
            [
                f"  Connected: {gatt.get('connected', False)}",
                f"  Services discovered: {len(gatt.get('services', []))}",
                f"  Known charger transport: {gatt.get('known_transport', False)}",
            ]
        )
        probes = gatt.get("read_only_protocol", {}).get("probes", {})
        succeeded = [name for name, value in probes.items() if value.get("success")]
        if probes:
            lines.append(f"  Read-only GET responses: {', '.join(succeeded) or 'none'}")
        if gatt.get("error"):
            lines.append(f"  GATT error: {gatt['error']}")
    return "\n".join(lines)


async def run(args: argparse.Namespace) -> int:
    """Run discovery, optional GATT capture, and report generation."""
    try:
        from bleak import BleakClient, BleakScanner
    except ImportError as error:
        raise RuntimeError(
            "bleak is required; install it with: python -m pip install bleak"
        ) from error

    if args.brush_pacer:
        print(
            "Disconnect Home Assistant and the Oral-B app, and unplug the iO "
            "Sense first; the toothbrush normally accepts only one active BLE "
            "connection."
        )
    print(f"Scanning for Bluetooth Low Energy devices ({args.scan_timeout:g}s)...")
    discovered = await BleakScanner.discover(
        timeout=args.scan_timeout, return_adv=True
    )
    candidates = build_candidates(discovered)
    chooser = choose_toothbrush_candidate if args.brush_pacer else choose_candidate
    candidate = chooser(candidates, args.address, interactive=sys.stdin.isatty())
    print(
        f"Selected {candidate.name} ({candidate.address}), RSSI {candidate.rssi} dBm"
    )

    try:
        bleak_version = importlib.metadata.version("bleak")
    except importlib.metadata.PackageNotFoundError:
        bleak_version = "unknown"
    report: dict[str, Any] = {
        "schema_version": 1,
        "tool": "iosense_probe",
        "tool_version": TOOL_VERSION,
        "mode": "brush_pacer" if args.brush_pacer else "iosense",
        "captured_at": utc_now(),
        "environment": {
            "platform": platform.platform(),
            "python": platform.python_version(),
            "bleak": bleak_version,
        },
        "safety": {
            "scan_only": args.scan_only,
            "post_or_set_commands_sent": False,
            "protocol_operations_allowed": [] if args.brush_pacer else ["GET"],
        },
        "advertisement": advertisement_report(candidate),
    }

    if args.brush_pacer:
        report["safety"].update(
            {
                "characteristic_value_writes_sent": False,
                "persistent_settings_changed": False,
                "temporary_notification_descriptor_writes": True,
            }
        )
        # The address is only needed for local device selection, not for an
        # issue attachment. Preserve the session bytes but redact the address.
        report["advertisement"]["address"] = "redacted"
        print(
            "Connecting read-only. Notification subscriptions may temporarily "
            "update Bluetooth CCCD descriptors; no setting/value writes are sent."
        )
        brush_report: dict[str, Any] = {
            "initial_reads": {},
            "final_reads": {},
            "annotation_notice": (
                "Decoded fields are positional hints only; use length and hex as "
                "the authoritative evidence."
            ),
        }
        report["brush_pacer"] = brush_report
        try:
            async with BleakClient(
                candidate.device, timeout=args.connect_timeout
            ) as client:
                brush_report["connected"] = bool(client.is_connected)
                capture = BrushPacerCapture(client)
                ended_reason = "capture_error"
                try:
                    start_error: BaseException | None = None
                    try:
                        await capture.start()
                    except Exception as error:
                        start_error = error

                    # Read after notification setup so no early brushing event is
                    # lost. Empty and all-zero replies remain distinguishable.
                    brush_report["initial_reads"] = await read_brush_snapshot(
                        client,
                        BRUSH_INITIAL_READS,
                        read_timeout=args.request_timeout,
                    )
                    brush_report["initial_reads_completed_at"] = utc_now()

                    if start_error is not None:
                        raise start_error
                    print("Ready. Start brushing now.")
                    print(
                        "Capture stops after the brush leaves running state, or "
                        f"after {args.session_timeout:g} seconds."
                    )
                    ended_reason = await capture.wait(
                        session_timeout=args.session_timeout,
                        end_grace=0,
                    )
                    # Re-read the configuration immediately after the terminal
                    # state (or at timeout), while the same connection is alive.
                    brush_report["final_reads"] = await read_brush_snapshot(
                        client,
                        BRUSH_FINAL_READS,
                        read_timeout=args.request_timeout,
                    )
                    brush_report["final_reads_completed_at"] = utc_now()
                    if ended_reason == "state_after_running":
                        await asyncio.sleep(2.0)
                except Exception as error:
                    brush_report["error"] = error_text(error)
                finally:
                    await capture.stop()
                    brush_report["session"] = capture.report(ended_reason)
        except Exception as error:
            brush_report["connected"] = False
            brush_report["error"] = error_text(error)
    elif not args.scan_only:
        print("Connecting to capture GATT and read-only values...")
        gatt: dict[str, Any] = {"connected": False, "services": []}
        report["gatt"] = gatt
        try:
            async with BleakClient(
                candidate.device, timeout=args.connect_timeout
            ) as client:
                gatt["connected"] = bool(client.is_connected)
                services, characteristic_uuids = await capture_gatt(
                    client, read_timeout=args.request_timeout
                )
                gatt["services"] = services
                gatt["known_transport"] = known_transport_present(
                    characteristic_uuids
                )
                if gatt["known_transport"] and not args.no_protocol:
                    print(
                        "Known charger transport found; running read-only GET probes..."
                    )
                    protocol = ReadOnlyProtocolProbe(
                        client,
                        request_timeout=args.request_timeout,
                        frame_delay=args.frame_delay,
                    )
                    protocol_report: dict[str, Any] = {
                        "operation": "GET only",
                        "probes": {},
                    }
                    gatt["read_only_protocol"] = protocol_report
                    try:
                        await protocol.start()
                        for name, command in READ_ONLY_PROBES:
                            protocol_report["probes"][name] = await protocol.get(
                                command
                            )
                    except Exception as error:
                        protocol_report["error"] = error_text(error)
                    finally:
                        await protocol.stop()
                        protocol_report["frames_written"] = protocol.frames_written
                        protocol_report["status_notifications"] = (
                            protocol.status_notifications
                        )
        except Exception as error:
            gatt["error"] = error_text(error)

    output = args.output or default_output_path(brush_pacer=args.brush_pacer)
    output.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print()
    print(console_summary(report))
    print(f"\nSaved report: {output.resolve()}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    """Build the command-line interface."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--address", help="specific BLE address or macOS CoreBluetooth UUID"
    )
    parser.add_argument(
        "--scan-timeout", type=float, default=15.0, help="scan seconds (default: 15)"
    )
    parser.add_argument(
        "--connect-timeout",
        type=float,
        default=20.0,
        help="connection timeout seconds (default: 20)",
    )
    parser.add_argument(
        "--request-timeout",
        type=float,
        default=5.0,
        help="per-read timeout seconds (default: 5)",
    )
    parser.add_argument(
        "--frame-delay",
        type=float,
        default=0.35,
        help="delay between read-only GET frames (default: 0.35)",
    )
    parser.add_argument(
        "--brush-pacer",
        action="store_true",
        help=(
            "capture one toothbrush pacer session from FF04/FF07/FF08/FF09 "
            "notifications plus FF02 and initial/final FF25/FF26/FF09 reads"
        ),
    )
    parser.add_argument(
        "--session-timeout",
        type=float,
        default=240.0,
        help="brush-session timeout seconds (default: 240)",
    )
    parser.add_argument(
        "--scan-only",
        action="store_true",
        help="capture advertisements only; never connect or write GATT frames",
    )
    parser.add_argument(
        "--no-protocol",
        action="store_true",
        help="connect and dump GATT but do not send read-only charger GET frames",
    )
    parser.add_argument("--output", type=Path, help="JSON report path")
    return parser


def main() -> int:
    """CLI entry point."""
    parser = build_parser()
    args = parser.parse_args()
    for field in (
        "scan_timeout",
        "connect_timeout",
        "request_timeout",
    ):
        if getattr(args, field) <= 0:
            parser.error(f"--{field.replace('_', '-')} must be greater than zero")
    if args.frame_delay < 0:
        parser.error("--frame-delay cannot be negative")
    if args.brush_pacer and args.session_timeout <= 0:
        parser.error("--session-timeout must be greater than zero")
    if args.brush_pacer and args.scan_only:
        parser.error("--brush-pacer cannot be combined with --scan-only")
    if args.brush_pacer and args.no_protocol:
        parser.error("--brush-pacer cannot be combined with --no-protocol")
    try:
        return asyncio.run(run(args))
    except (OSError, RuntimeError, asyncio.TimeoutError) as error:
        print(f"error: {error_text(error)}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
