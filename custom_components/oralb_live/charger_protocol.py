"""Pure decoders for the Oral-B iO Sense charger BLE bridge."""

from __future__ import annotations

from dataclasses import dataclass
from enum import IntEnum
from typing import Any

IOSENSE_SERVICE_UUID = "a0f03e00-5047-4d53-8208-4f72616c2d42"
IOSENSE_COMMAND_UUID = "a0f03c00-5047-4d53-8208-4f72616c2d42"
IOSENSE_READ_UUID = "a0f03c01-5047-4d53-8208-4f72616c2d42"
IOSENSE_WRITE_UUID = "a0f03c02-5047-4d53-8208-4f72616c2d42"
IOSENSE_STATUS_UUID = "a0f03c03-5047-4d53-8208-4f72616c2d42"
IOSENSE_PROTOCOL_END = b"\xe0"


class ChargerCommand(IntEnum):
    """Read-only commands used by the integration."""

    DEVICE_ID = 0x03
    WIFI_STATUS = 0x14
    WIFI_RSSI = 0x15
    IOT_STATUS = 0x16
    INTERNET_TYPE = 0x1A
    TIMEZONE = 0x1B
    FW_VERSION = 0x1E
    UPTIME = 0x1F
    FREE_HEAP = 0x20
    HW_VERSION = 0x25
    SERVER_MODE = 0x26
    CLOCK_BRIGHTNESS = 0x30
    CLOCK_DISPLAY_MODE = 0x31
    BRUSH_STATUS = 0x33
    BRUSH_CONNECTION_POLICY = 0x34
    RING_COLOR = 0x36
    BRUSH_PASSTHROUGH = 0x37
    BRUSH_DATA = 0x39
    SESSION_STATUS = 0x3A
    AUTO_UPDATE = 0x3B
    TOUCHPAD_STATUS = 0x3D
    DATE_SHOW_MODE = 0x3F
    NIGHT_LIGHT_MODE = 0x42
    CLOCK_TEXT = 0x44
    BRUSH_PAIRED = 0x46


CHARGER_SNAPSHOT_COMMANDS = (
    ChargerCommand.FW_VERSION,
    ChargerCommand.HW_VERSION,
    ChargerCommand.SERVER_MODE,
    ChargerCommand.DEVICE_ID,
    ChargerCommand.WIFI_STATUS,
    ChargerCommand.WIFI_RSSI,
    ChargerCommand.IOT_STATUS,
    ChargerCommand.INTERNET_TYPE,
    ChargerCommand.TIMEZONE,
    ChargerCommand.UPTIME,
    ChargerCommand.CLOCK_BRIGHTNESS,
    ChargerCommand.CLOCK_DISPLAY_MODE,
    ChargerCommand.CLOCK_TEXT,
    ChargerCommand.DATE_SHOW_MODE,
    ChargerCommand.RING_COLOR,
    ChargerCommand.NIGHT_LIGHT_MODE,
    ChargerCommand.AUTO_UPDATE,
    ChargerCommand.BRUSH_CONNECTION_POLICY,
    ChargerCommand.BRUSH_STATUS,
    ChargerCommand.SESSION_STATUS,
    ChargerCommand.TOUCHPAD_STATUS,
    ChargerCommand.BRUSH_PAIRED,
)


ENUMS: dict[ChargerCommand, dict[int, str]] = {
    ChargerCommand.WIFI_STATUS: {
        0: "none",
        1: "disabled",
        2: "connecting",
        3: "not_connected",
        4: "connected_with_internet",
        5: "connected_without_internet",
    },
    ChargerCommand.IOT_STATUS: {
        0: "none",
        1: "connecting",
        2: "not_connected",
        3: "connected",
    },
    ChargerCommand.INTERNET_TYPE: {
        0: "none",
        1: "wifi",
        2: "cellular",
        3: "ethernet",
        0xFF: "unknown",
    },
    ChargerCommand.SERVER_MODE: {
        0: "full",
        1: "limited",
        2: "wifi_only",
        3: "aws_only",
    },
    ChargerCommand.CLOCK_DISPLAY_MODE: {0: "24_hour", 1: "12_hour"},
    ChargerCommand.BRUSH_STATUS: {
        0: "not_connected",
        1: "pre_run",
        2: "idle",
        3: "charging",
        4: "run",
        0xFF: "unknown",
    },
    ChargerCommand.BRUSH_CONNECTION_POLICY: {
        0: "allowed",
        1: "temporarily_forbidden",
        2: "forbidden",
    },
    ChargerCommand.SESSION_STATUS: {
        0: "inactive",
        1: "active_running",
        2: "active_idle",
    },
    ChargerCommand.AUTO_UPDATE: {0: "enabled", 1: "disabled", 0xFF: "unknown"},
    ChargerCommand.DATE_SHOW_MODE: {
        0: "disabled",
        1: "month_day",
        2: "day_month",
        0xFF: "unknown",
    },
    ChargerCommand.NIGHT_LIGHT_MODE: {
        0: "disabled",
        1: "solid",
        2: "breathing",
        3: "rainbow",
        4: "cool",
        5: "custom",
        0xFF: "unknown",
    },
}

ACTIVE_BRUSH_SESSION_STATES = frozenset({"pre_run", "run"})
QUIET_BRUSH_STATES = frozenset({"not_connected", "idle", "charging"})


def resolve_charger_session_running(
    session_status: str | None,
    brush_status: str | None,
    currently_running: bool = False,
) -> bool:
    """Resolve charger session state, preferring the reliable brush status.

    Tested iO Sense firmware can leave SESSION_STATUS at ``inactive`` while a
    real brushing session reports ``pre_run`` through BRUSH_STATUS. Conversely,
    BRUSH_STATUS changes to idle/charging promptly at the end of a session.
    """
    if brush_status in ACTIVE_BRUSH_SESSION_STATES:
        return True
    if brush_status in QUIET_BRUSH_STATES:
        return False
    # Any non-null value outside the decoded active/quiet sets is an
    # inconclusive sample, not evidence that brushing stopped. Preserve an
    # already-running stream rather than let the firmware's known-stale
    # SESSION_STATUS split it into two sessions. ``None`` still permits the
    # native session-status fallback before BRUSH_STATUS has been available.
    if currently_running and brush_status is not None:
        return True
    if session_status == "active_running":
        return True
    if session_status in {"active_idle", "inactive"}:
        return False
    return currently_running


def charger_live_auxiliary(
    tick: int,
    *,
    mode_observed: bool,
    brush_status_every_ticks: int,
    battery_every_ticks: int,
) -> str:
    """Return the single auxiliary request for a charger live-loop tick."""
    if tick > 0 and tick % brush_status_every_ticks == 0:
        return "BRUSH_STATUS"
    if tick == 0:
        return "FF05"
    if tick == 1:
        return "FF07"
    if tick == 2:
        return "FF26"
    if tick == 3:
        return "FF2D"
    # Retry the mode read without starving timer/pacer anchors if its first
    # response was lost. Once observed, no extra request is needed.
    if not mode_observed and tick >= 6 and (tick - 6) % 10 == 0:
        return "FF07"
    # Status ticks collide with the regular battery interval, so place each
    # slow battery refresh in the immediately preceding auxiliary slot.
    if tick > 0 and (tick + 1) % battery_every_ticks == 0:
        return "FF05"
    return "FF08" if tick % 2 == 0 else "FF09"


@dataclass(frozen=True)
class ChargerPacket:
    command: ChargerCommand | None
    command_id: int | None
    operation: int | None
    value: Any
    payload: bytes
    raw: bytes
    error: str | None = None


def normalize_mac(value: str) -> str | None:
    """Return twelve uppercase hex characters for a MAC-like value."""
    normalized = "".join(
        character for character in value if character.isalnum()
    ).upper()
    if len(normalized) != 12 or any(
        character not in "0123456789ABCDEF" for character in normalized
    ):
        return None
    return normalized


def decode_charger_advertisement(payload: bytes | bytearray) -> dict[str, Any]:
    """Decode the 14-byte manufacturer value advertised by iO Sense."""
    raw = bytes(payload)
    if len(raw) != 14:
        return {"raw": raw.hex(), "error": f"expected 14 bytes, got {len(raw)}"}
    status = raw[13]
    return {
        "protocol_version": raw[0],
        "device_type": "stargate" if raw[1] == 0xA2 else f"type_0x{raw[1]:02x}",
        "firmware": f"{raw[2]}.{raw[3]}.{raw[4]}",
        "server_mode": ENUMS[ChargerCommand.SERVER_MODE].get(raw[5], f"mode_{raw[5]}"),
        "mac": ":".join(f"{value:02X}" for value in raw[6:12]),
        "body_color": {0: "white", 1: "black"}.get(raw[12], f"color_{raw[12]}"),
        "connection_bits": status,
        "wifi_connected": bool(status & 0x01),
        "internet_connected": bool(status & 0x02),
        "cloud_connected": bool(status & 0x04),
        "brush_connected": bool(status & 0x08),
        "brush_charging": bool(status & 0x10),
        "touchpad_active": bool(status & 0x20),
        "brush_paired": bool(status & 0x40),
        "demo_mode": bool(status & 0x80),
    }


def _text(payload: bytes) -> str:
    return payload.decode("utf-8", errors="replace").replace("\x00", "")


def _brush_data(payload: bytes) -> dict[str, Any]:
    result: dict[str, Any] = {"raw": payload.hex(), "length": len(payload)}
    if len(payload) >= 7:
        result["brush_mac"] = ":".join(f"{value:02X}" for value in payload[1:7])
    if len(payload) > 40:
        result["model_id"] = payload[16]
        result["protocol_version"] = payload[39]
        result["firmware_revision"] = payload[40]
    if len(payload) > 59:
        # iO/Sonos controller metadata. These offsets mirror the current
        # Android SDK's BRUSH_DATA parser after removing the two-byte charger
        # command header. The app displays firmware as
        # second-controller.software.media-content (zero-padded).
        result.update(
            {
                "hardware_version": payload[41],
                "bootloader_version": payload[43],
                "media_content_version": payload[46],
                "hardware_configuration": payload[47],
                "memory_map_version": payload[49],
                "info_sector_version": payload[52],
                "second_controller_version": payload[59],
            }
        )
    return result


def decode_passthrough_records(payload: bytes | bytearray) -> list[dict[str, Any]]:
    """Decode one or more charger-mediated brush read records."""
    raw = bytes(payload)
    records: list[dict[str, Any]] = []
    offset = 0
    while offset < len(raw):
        remaining = raw[offset:]
        if not any(remaining):
            break
        if len(remaining) < 5:
            records.append({"error": "truncated_header", "raw": remaining.hex()})
            break
        length = remaining[4]
        size = 5 + length
        if len(remaining) < size:
            records.append({"error": "truncated_payload", "raw": remaining.hex()})
            break
        record = remaining[:size]
        records.append(
            {
                "short_uuid": f"{record[1]:02X}{record[0]:02X}",
                "operation": record[2],
                "success": record[3] == 1,
                "data": record[5:],
                "raw": record,
            }
        )
        offset += size
    return records


def decode_charger_read(data: bytes | bytearray) -> ChargerPacket:
    """Decode a charger read-characteristic notification."""
    raw = bytes(data)
    if len(raw) < 2:
        return ChargerPacket(None, None, None, None, b"", raw, "short_packet")
    command_id, operation = raw[0], raw[1]
    payload = raw[2:]
    try:
        command = ChargerCommand(command_id)
    except ValueError:
        command = None
    value: Any = payload.hex()
    if command in ENUMS and payload:
        value = ENUMS[command].get(payload[0], f"unknown_0x{payload[0]:02x}")
    elif command == ChargerCommand.WIFI_RSSI and payload:
        value = payload[0] - 127
    elif command in {ChargerCommand.DEVICE_ID, ChargerCommand.TIMEZONE}:
        value = _text(payload)
    elif command == ChargerCommand.FW_VERSION and len(payload) >= 3:
        value = ".".join(str(part) for part in payload[:3])
    elif command == ChargerCommand.HW_VERSION and payload:
        value = payload[0]
    elif (
        command in {ChargerCommand.UPTIME, ChargerCommand.FREE_HEAP}
        and len(payload) >= 4
    ):
        value = int.from_bytes(payload[:4], "little")
    elif command == ChargerCommand.CLOCK_BRIGHTNESS and payload:
        value = payload[0] if payload[0] <= 100 else None
    elif command == ChargerCommand.CLOCK_TEXT:
        if len(payload) >= 5 and payload[4]:
            value = _text(payload[:2] + b":" + payload[2:4])
        else:
            value = _text(payload[:4])
    elif command == ChargerCommand.RING_COLOR and len(payload) >= 3:
        value = f"#{payload[0]:02X}{payload[1]:02X}{payload[2]:02X}"
    elif (
        command in {ChargerCommand.TOUCHPAD_STATUS, ChargerCommand.BRUSH_PAIRED}
        and payload
    ):
        value = payload[0] == 1
    elif command == ChargerCommand.BRUSH_DATA:
        value = _brush_data(payload)
    elif command == ChargerCommand.BRUSH_PASSTHROUGH:
        value = decode_passthrough_records(payload)
    return ChargerPacket(command, command_id, operation, value, payload, raw)


def build_charger_get(command: ChargerCommand) -> bytes:
    """Build a read-only charger GET header."""
    if command == ChargerCommand.BRUSH_PASSTHROUGH:
        raise ValueError("passthrough uses a POST header")
    return bytes((0xC0, command))


def build_passthrough_read(short_uuid: str) -> bytes:
    """Build the vendor-compatible length-prefixed brush read record."""
    normalized = short_uuid.strip().upper()
    if len(normalized) != 4:
        raise ValueError("short UUID must contain four hexadecimal characters")
    try:
        high, low = bytes.fromhex(normalized)
    except ValueError as exc:
        raise ValueError("short UUID must contain four hexadecimal characters") from exc
    return bytes((low, high, 0x01, 0x00))
