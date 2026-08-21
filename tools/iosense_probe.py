#!/usr/bin/env python3
"""Discover an iO Sense, capture its advertisement, and probe it read-only.

The default command connects to the selected charger, enumerates GATT, reads
characteristics marked readable, attempts read-only descriptor reads, and—if
the known iO Sense command transport exists—sends a small set of GET requests.
It never sends POST/SET commands or writes a command payload.
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


TOOL_VERSION = "1"
ORALB_MANUFACTURER_ID = 220
IOSENSE_SERVICE_UUID = "a0f03e00-5047-4d53-8208-4f72616c2d42"
IOSENSE_COMMAND_UUID = "a0f03c00-5047-4d53-8208-4f72616c2d42"
IOSENSE_READ_UUID = "a0f03c01-5047-4d53-8208-4f72616c2d42"
IOSENSE_WRITE_UUID = "a0f03c02-5047-4d53-8208-4f72616c2d42"
IOSENSE_STATUS_UUID = "a0f03c03-5047-4d53-8208-4f72616c2d42"
IOSENSE_PROTOCOL_END = b"\xe0"
GET_OPERATION = 0xC0

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


def default_output_path() -> Path:
    """Return a timestamped report path."""
    timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    return Path(f"iosense-probe-{timestamp}.json")


def console_summary(report: dict[str, Any]) -> str:
    """Build a compact issue-friendly summary."""
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

    print(f"Scanning for Bluetooth Low Energy devices ({args.scan_timeout:g}s)...")
    discovered = await BleakScanner.discover(
        timeout=args.scan_timeout, return_adv=True
    )
    candidates = build_candidates(discovered)
    candidate = choose_candidate(
        candidates, args.address, interactive=sys.stdin.isatty()
    )
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
        "captured_at": utc_now(),
        "environment": {
            "platform": platform.platform(),
            "python": platform.python_version(),
            "bleak": bleak_version,
        },
        "safety": {
            "scan_only": args.scan_only,
            "post_or_set_commands_sent": False,
            "protocol_operations_allowed": ["GET"],
        },
        "advertisement": advertisement_report(candidate),
    }

    if not args.scan_only:
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

    output = args.output or default_output_path()
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
    try:
        return asyncio.run(run(args))
    except (OSError, RuntimeError, asyncio.TimeoutError) as error:
        print(f"error: {error_text(error)}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
