#!/usr/bin/env python3
"""Standalone, guarded tester for the iO Sense charger night light.

This utility deliberately does not import the Home Assistant integration.
Run ``frames`` for an entirely offline preview. Live mutations require the
explicit ``--apply`` flag and are verified by reading the value back.
"""

from __future__ import annotations

import argparse
import asyncio
from datetime import datetime, timezone
from enum import IntEnum
import json
from pathlib import Path
import re
import sys
from typing import Any


SERVICE_UUID = "a0f03e00-5047-4d53-8208-4f72616c2d42"
COMMAND_UUID = "a0f03c00-5047-4d53-8208-4f72616c2d42"
READ_UUID = "a0f03c01-5047-4d53-8208-4f72616c2d42"
WRITE_UUID = "a0f03c02-5047-4d53-8208-4f72616c2d42"
STATUS_UUID = "a0f03c03-5047-4d53-8208-4f72616c2d42"
PROTOCOL_END = b"\xe0"

CHARACTERISTIC_NAMES = {
    COMMAND_UUID: "C00 command",
    READ_UUID: "C01 read",
    WRITE_UUID: "C02 payload",
    STATUS_UUID: "C03 status",
}

GET = 0xC0
POST = 0xC1
RING_COLOR = 0x36
NIGHT_LIGHT_MODE = 0x42


class NightLightMode(IntEnum):
    """Modes reconstructed from the official charger SDK."""

    disabled = 0
    solid = 1
    breathing = 2
    rainbow = 3
    cool = 4
    custom = 5


MODE_NAMES = ", ".join(NightLightMode.__members__)
ACTIVE_MODE_NAMES = ", ".join(
    mode.name for mode in NightLightMode if mode is not NightLightMode.disabled
)


def parse_mode(value: str) -> NightLightMode:
    """Parse a human-readable night-light mode."""
    try:
        return NightLightMode[value.lower()]
    except KeyError as exc:
        raise argparse.ArgumentTypeError(f"mode must be one of: {MODE_NAMES}") from exc


def parse_active_mode(value: str) -> NightLightMode:
    """Parse a mode that actually enables the night light."""
    mode = parse_mode(value)
    if mode is NightLightMode.disabled:
        raise argparse.ArgumentTypeError(
            f"enable mode must be one of: {ACTIVE_MODE_NAMES}"
        )
    return mode


def parse_color(value: str) -> bytes:
    """Parse #RRGGBB/RRGGBB into the charger's three-byte RGB payload."""
    match = re.fullmatch(r"#?([0-9a-fA-F]{6})", value.strip())
    if match is None:
        raise argparse.ArgumentTypeError("color must be #RRGGBB or RRGGBB")
    return bytes.fromhex(match.group(1))


def color_text(payload: bytes) -> str:
    """Format a three-byte RGB payload."""
    if len(payload) != 3:
        raise ValueError(f"expected 3 RGB bytes, got {len(payload)}")
    return f"#{payload.hex().upper()}"


def get_frames(command: int) -> tuple[bytes, bytes]:
    """Return the two writes in a charger protocol-v2 GET."""
    return bytes((GET, command)), PROTOCOL_END


def post_frames(command: int, payload: bytes) -> tuple[bytes, bytes, bytes]:
    """Return command, payload, and terminator writes for a POST."""
    return bytes((POST, command)), bytes(payload), PROTOCOL_END


def describe_frames(command: int, payload: bytes | None = None) -> list[dict[str, str]]:
    """Create a printable wire-frame plan."""
    if payload is None:
        header, end = get_frames(command)
        return [
            {"characteristic": "C00 command", "hex": header.hex()},
            {"characteristic": "C00 command", "hex": end.hex()},
        ]
    header, body, end = post_frames(command, payload)
    return [
        {"characteristic": "C00 command", "hex": header.hex()},
        {"characteristic": "C02 payload", "hex": body.hex()},
        {"characteristic": "C00 command", "hex": end.hex()},
    ]


class ChargerSession:
    """Small transaction client for the two audited night-light commands."""

    def __init__(
        self,
        client: Any,
        *,
        timeout: float,
        frame_delay: float,
        settle_delay: float,
    ) -> None:
        self.client = client
        self.timeout = timeout
        self.frame_delay = frame_delay
        self.settle_delay = settle_delay
        self._read_waiters: dict[tuple[int, int], asyncio.Future[bytes]] = {}
        self._status_waiters: dict[tuple[int, int], asyncio.Future[bytes]] = {}
        self._lock = asyncio.Lock()

    async def start(self) -> None:
        await self.client.start_notify(READ_UUID, self._on_read)
        await self.client.start_notify(STATUS_UUID, self._on_status)

    async def stop(self) -> None:
        await self.client.stop_notify(STATUS_UUID)
        await self.client.stop_notify(READ_UUID)

    def _on_read(self, _characteristic: Any, value: bytearray) -> None:
        raw = bytes(value)
        if len(raw) < 2:
            return
        waiter = self._read_waiters.get((raw[0], raw[1]))
        if waiter is not None and not waiter.done():
            waiter.set_result(raw)

    def _on_status(self, _characteristic: Any, value: bytearray) -> None:
        raw = bytes(value)
        if len(raw) < 3:
            return
        waiter = self._status_waiters.get((raw[0], raw[1]))
        if waiter is not None and not waiter.done():
            waiter.set_result(raw)

    async def _write(self, characteristic: str, value: bytes) -> None:
        label = CHARACTERISTIC_NAMES.get(characteristic, characteristic)
        print(f"write {label}: {value.hex()}")
        await self.client.write_gatt_char(characteristic, value, response=True)

    async def get(self, command: int) -> bytes:
        """Read one command and return its payload."""
        async with self._lock:
            key = (command, GET)
            waiter = asyncio.get_running_loop().create_future()
            self._read_waiters[key] = waiter
            try:
                header, end = get_frames(command)
                await self._write(COMMAND_UUID, header)
                await asyncio.sleep(self.frame_delay)
                await self._write(COMMAND_UUID, end)
                raw = await asyncio.wait_for(waiter, timeout=self.timeout)
                print(f"read C01: {raw.hex()}")
                return raw[2:]
            finally:
                self._read_waiters.pop(key, None)
                if not waiter.done():
                    waiter.cancel()

    async def post(self, command: int, payload: bytes) -> None:
        """Write one command, require a success status, then allow it to settle."""
        async with self._lock:
            key = (command, POST)
            waiter = asyncio.get_running_loop().create_future()
            self._status_waiters[key] = waiter
            try:
                header, body, end = post_frames(command, payload)
                await self._write(COMMAND_UUID, header)
                await asyncio.sleep(self.frame_delay)
                await self._write(WRITE_UUID, body)
                await asyncio.sleep(self.frame_delay)
                await self._write(COMMAND_UUID, end)
                raw = await asyncio.wait_for(waiter, timeout=self.timeout)
                print(f"status C03: {raw.hex()}")
                if raw[2] != 1:
                    raise RuntimeError(
                        f"charger rejected command 0x{command:02X}: status {raw.hex()}"
                    )
                await asyncio.sleep(self.settle_delay)
            finally:
                self._status_waiters.pop(key, None)
                if not waiter.done():
                    waiter.cancel()

    async def state(self) -> tuple[bytes, NightLightMode]:
        color = await self.get(RING_COLOR)
        if len(color) != 3:
            raise RuntimeError(f"unexpected ring-color payload: {color.hex()}")
        mode_payload = await self.get(NIGHT_LIGHT_MODE)
        if len(mode_payload) < 1:
            raise RuntimeError("night-light mode response had no payload")
        try:
            mode = NightLightMode(mode_payload[0])
        except ValueError as exc:
            raise RuntimeError(
                f"unknown night-light mode 0x{mode_payload[0]:02X}"
            ) from exc
        return color, mode

    async def set_color(self, color: bytes) -> None:
        await self.post(RING_COLOR, color)
        actual = await self.get(RING_COLOR)
        if actual != color:
            raise RuntimeError(
                f"ring-color verification failed: wanted {color_text(color)}, "
                f"read {actual.hex()}"
            )
        print(f"verified ring color: {color_text(actual)}")

    async def set_mode(self, mode: NightLightMode) -> None:
        expected = bytes((mode,))
        await self.post(NIGHT_LIGHT_MODE, expected)
        actual = await self.get(NIGHT_LIGHT_MODE)
        if actual[:1] != expected:
            raise RuntimeError(
                f"night-light verification failed: wanted {mode.name}, "
                f"read {actual.hex()}"
            )
        print(f"verified night-light mode: {mode.name}")


def print_state(color: bytes, mode: NightLightMode, *, label: str) -> None:
    print(f"{label}: color={color_text(color)}, mode={mode.name} ({mode.value})")


def save_backup(
    path: Path, address: str, color: bytes, mode: NightLightMode
) -> None:
    backup = {
        "created_at": datetime.now(timezone.utc).isoformat(),
        "address": address,
        "ring_color": color_text(color),
        "night_light_mode": mode.name,
    }
    path.write_text(json.dumps(backup, indent=2) + "\n", encoding="utf-8")
    print(f"saved original state: {path.resolve()}")


def load_backup(path: Path) -> tuple[bytes, NightLightMode]:
    value = json.loads(path.read_text(encoding="utf-8"))
    return parse_color(value["ring_color"]), NightLightMode[value["night_light_mode"]]


def default_backup_path() -> Path:
    timestamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
    return Path(f"iosense-night-light-backup-{timestamp}.json")


async def find_charger(address: str | None, timeout: float) -> Any:
    try:
        from bleak import BleakScanner
    except ImportError as exc:
        raise RuntimeError("bleak is required: python3 -m pip install bleak") from exc

    print(f"scanning for iO Sense ({timeout:g}s)...")
    discovered = await BleakScanner.discover(timeout=timeout, return_adv=True)
    matches = []
    for device, advertisement in discovered.values():
        advertised_address = device.address.lower()
        wanted_address = address.lower() if address else None
        service_uuids = {item.lower() for item in advertisement.service_uuids}
        name = device.name or advertisement.local_name or ""
        if wanted_address and advertised_address == wanted_address:
            return device
        if name == "iO Sense" or SERVICE_UUID in service_uuids:
            matches.append(device)
    if address:
        raise RuntimeError(f"iO Sense address {address!r} was not found")
    if not matches:
        raise RuntimeError("no iO Sense charger was found")
    if len(matches) > 1:
        choices = ", ".join(device.address for device in matches)
        raise RuntimeError(f"multiple chargers found ({choices}); pass --address")
    return matches[0]


async def restore_state(
    session: ChargerSession, color: bytes, mode: NightLightMode
) -> None:
    """Restore color first and mode last, then verify the combined state."""
    print("restoring saved state...")
    await session.set_color(color)
    await session.set_mode(mode)
    actual_color, actual_mode = await session.state()
    if (actual_color, actual_mode) != (color, mode):
        raise RuntimeError("combined restore verification failed")
    print_state(actual_color, actual_mode, label="restored")


async def run_live(args: argparse.Namespace) -> int:
    try:
        from bleak import BleakClient
    except ImportError as exc:
        raise RuntimeError("bleak is required: python3 -m pip install bleak") from exc

    device = await find_charger(args.address, args.scan_timeout)
    print(f"connecting to {device.name or 'iO Sense'} ({device.address})...")
    async with BleakClient(device, timeout=args.connect_timeout) as client:
        session = ChargerSession(
            client,
            timeout=args.request_timeout,
            frame_delay=args.frame_delay,
            settle_delay=args.settle_delay,
        )
        await session.start()
        try:
            original_color, original_mode = await session.state()
            print_state(original_color, original_mode, label="current")
            if args.action == "status":
                return 0

            if not args.apply:
                if args.action == "set-color":
                    print(f"planned ring color: {color_text(args.color)}")
                elif args.action in {"set-mode", "enable"}:
                    print(f"planned night-light mode: {args.mode.name}")
                elif args.action == "disable":
                    print("planned night-light mode: disabled")
                elif args.action == "roundtrip":
                    print(
                        f"planned roundtrip: {color_text(args.color)}, "
                        f"mode={args.mode.name}, hold={args.hold:g}s, then restore"
                    )
                elif args.action == "restore":
                    print(f"planned restore from: {args.file}")
                print("dry run only; no values were changed (add --apply to write)")
                return 0

            restore_target = None
            if args.action == "restore":
                # Load before writing the safety backup, particularly if a
                # caller accidentally supplies the same path for both.
                restore_target = load_backup(args.file)
            backup_path = args.backup or default_backup_path()
            if args.action == "restore" and backup_path.resolve() == args.file.resolve():
                raise RuntimeError("--backup must not overwrite the restore input file")
            save_backup(backup_path, device.address, original_color, original_mode)

            if args.action == "set-color":
                await session.set_color(args.color)
            elif args.action in {"set-mode", "enable"}:
                await session.set_mode(args.mode)
            elif args.action == "disable":
                await session.set_mode(NightLightMode.disabled)
            elif args.action == "restore":
                assert restore_target is not None
                color, mode = restore_target
                await restore_state(session, color, mode)
            elif args.action == "roundtrip":
                changed = False
                try:
                    # A POST might take effect even if its acknowledgement or
                    # read-back is lost, so restoration must be armed first.
                    changed = True
                    await session.set_color(args.color)
                    await session.set_mode(args.mode)
                    print(f"holding test state for {args.hold:g}s...")
                    await asyncio.sleep(args.hold)
                finally:
                    if changed:
                        await restore_state(session, original_color, original_mode)
            else:  # pragma: no cover - argparse constrains this
                raise RuntimeError(f"unsupported action {args.action}")

            final_color, final_mode = await session.state()
            print_state(final_color, final_mode, label="final")
            return 0
        finally:
            await session.stop()


def add_mutation_options(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--apply",
        action="store_true",
        help="perform the write; without this flag the command is a live dry run",
    )
    parser.add_argument(
        "--backup",
        type=Path,
        help="backup path (default: timestamped JSON in the current directory)",
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--address", help="BLE address or macOS CoreBluetooth UUID")
    parser.add_argument("--scan-timeout", type=float, default=8.0)
    parser.add_argument("--connect-timeout", type=float, default=20.0)
    parser.add_argument("--request-timeout", type=float, default=5.0)
    parser.add_argument(
        "--frame-delay",
        type=float,
        default=0.35,
        help="delay between protocol writes (default: 0.35s)",
    )
    parser.add_argument(
        "--settle-delay",
        type=float,
        default=1.0,
        help="delay after an acknowledged POST before verification (default: 1s)",
    )
    subparsers = parser.add_subparsers(dest="action", required=True)

    subparsers.add_parser("status", help="read RGB and mode without changing them")

    frames = subparsers.add_parser("frames", help="offline wire-frame preview")
    frames.add_argument("--color", type=parse_color)
    frames.add_argument("--mode", type=parse_mode, metavar=f"{{{MODE_NAMES}}}")

    set_color = subparsers.add_parser("set-color", help="set and verify RGB")
    set_color.add_argument("color", type=parse_color)
    add_mutation_options(set_color)

    set_mode = subparsers.add_parser("set-mode", help="set and verify any mode")
    set_mode.add_argument("mode", type=parse_mode, metavar=f"{{{MODE_NAMES}}}")
    add_mutation_options(set_mode)

    enable = subparsers.add_parser("enable", help="enable using solid or another active mode")
    enable.add_argument(
        "--mode",
        type=parse_active_mode,
        metavar=f"{{{ACTIVE_MODE_NAMES}}}",
        default=NightLightMode.solid,
    )
    add_mutation_options(enable)

    disable = subparsers.add_parser("disable", help="set mode to disabled")
    add_mutation_options(disable)

    restore = subparsers.add_parser("restore", help="restore color/mode from backup JSON")
    restore.add_argument("file", type=Path)
    add_mutation_options(restore)

    roundtrip = subparsers.add_parser(
        "roundtrip", help="apply a test state, hold it, and automatically restore"
    )
    roundtrip.add_argument("color", type=parse_color)
    roundtrip.add_argument(
        "--mode",
        type=parse_active_mode,
        metavar=f"{{{ACTIVE_MODE_NAMES}}}",
        default=NightLightMode.solid,
    )
    roundtrip.add_argument("--hold", type=float, default=5.0)
    add_mutation_options(roundtrip)

    return parser


def show_offline_frames(args: argparse.Namespace) -> int:
    plans: dict[str, list[dict[str, str]]] = {
        "get_ring_color": describe_frames(RING_COLOR),
        "get_night_light_mode": describe_frames(NIGHT_LIGHT_MODE),
    }
    if args.color is not None:
        plans["post_ring_color"] = describe_frames(RING_COLOR, args.color)
    if args.mode is not None:
        plans["post_night_light_mode"] = describe_frames(
            NIGHT_LIGHT_MODE, bytes((args.mode,))
        )
    print(json.dumps(plans, indent=2))
    return 0


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    if args.frame_delay < 0 or args.settle_delay < 0:
        parser.error("delays cannot be negative")
    if getattr(args, "hold", 0) < 0:
        parser.error("--hold cannot be negative")
    if args.action == "frames":
        return show_offline_frames(args)
    try:
        return asyncio.run(run_live(args))
    except (OSError, RuntimeError, asyncio.TimeoutError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
