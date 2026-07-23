"""Pure Oral-B protocol decoders.

Keep byte parsing here independent of Home Assistant so captures can be
regression-tested without installing Home Assistant.
"""

from __future__ import annotations

from typing import Any


def parse_battery_status(
    payload: bytes | bytearray,
) -> dict[str, int | float]:
    """Decode the fields available in ff05.

    Protocol 6 added remaining seconds. Protocol 8 extended the payload with
    voltage, current and battery temperature. Length checks keep this safe for
    older brushes.
    """
    result: dict[str, int | float] = {}
    if not payload:
        return result

    if 0 <= payload[0] <= 100:
        result["battery"] = payload[0]

    if len(payload) >= 3:
        seconds = int.from_bytes(payload[1:3], "little")
        if seconds != 0xFFFF:
            result["battery_time_remaining"] = seconds

    if len(payload) >= 5:
        millivolts = int.from_bytes(payload[3:5], "little")
        if 0 < millivolts < 10000:
            result["battery_voltage"] = millivolts / 1000

    if len(payload) >= 7:
        milliamperes = int.from_bytes(payload[5:7], "little")
        if milliamperes != 0xFFFF:
            result["battery_current"] = milliamperes

    if len(payload) >= 8:
        temperature = int.from_bytes(payload[7:8], "little", signed=True)
        if -40 <= temperature <= 125:
            result["battery_temperature"] = temperature

    return result


def parse_device_info(payload: bytes | bytearray) -> dict[str, int]:
    """Decode ff02, ordered as model, protocol and firmware revision."""
    if len(payload) < 3:
        return {}
    return {
        "model_id": payload[0],
        "protocol_version": payload[1],
        "firmware_revision": payload[2],
    }


def parse_pacer(payload: bytes | bytearray) -> dict[str, Any]:
    """Decode ff26 per-sector target times."""
    sector_times = [value for value in payload if 0 < value < 0xFF]
    if not sector_times:
        return {}
    return {
        "number_of_sectors": len(sector_times),
        "sector_times": sector_times,
        "target_duration": sum(sector_times),
    }


def parse_refill_remainder(payload: bytes | bytearray) -> dict[str, int]:
    """Decode ff2d brush-head refill remainder."""
    if len(payload) < 5:
        return {}

    days = int.from_bytes(payload[1:3], "little")
    brushing_seconds = int.from_bytes(payload[3:5], "little")
    result = {"refill_state_raw": payload[0]}
    if days != 0xFFFF:
        result["refill_days"] = days
    if brushing_seconds != 0xFFFF:
        result["refill_brushing_time"] = brushing_seconds
    return result


def parse_available_modes(payload: bytes | bytearray) -> list[int]:
    """Return unique mode identifiers from ff25, preserving brush order."""
    modes: list[int] = []
    for value in payload:
        if value != 0xFF and value not in modes:
            modes.append(value)
    return modes


def decode_sector(
    raw: int, total: int | None, configured_total: int | None
) -> tuple[str, int | None, int | None]:
    """Decode the low three quadrant bits and the total-sector hint."""
    decoded_total = (total or 0) & 0x07
    if not decoded_total:
        decoded_total = configured_total

    quadrant = raw & 0x07
    if quadrant == 0:
        return "no_sector", None, decoded_total
    if quadrant == 7:
        quadrant = decoded_total or 4
    return f"sector_{quadrant}", quadrant, decoded_total
