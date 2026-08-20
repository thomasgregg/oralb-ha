"""Pure Oral-B protocol decoders.

Keep byte parsing here independent of Home Assistant so captures can be
regression-tested without installing Home Assistant.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class DashboardRecord:
    """One timestamped brush motion/gyroscope sample."""

    timestamp: int
    gyro_x: int
    gyro_y: int
    gyro_z: int
    motion_x: int
    motion_y: int
    motion_z: int


def parse_comino_sensor_snapshot(
    payload: bytes | bytearray,
) -> tuple[DashboardRecord, ...]:
    """Decode the two Comino IMU samples returned by a brush FF0D read.

    FF0D stores the newest record first. Bytes 18-19 identify the payload as
    Comino motion data; the two records themselves use the same axis order and
    scale as the FF0E dashboard stream.
    """
    raw = bytes(payload)
    if len(raw) != 20:
        raise ValueError("Comino sensor snapshot must contain 20 bytes")
    if raw[18:20] != b"\x10\x80":
        raise ValueError(f"sensor snapshot is not Comino data: 0x{raw[18:20].hex()}")

    records = []
    for offset in (0, 8):
        record = raw[offset : offset + 8]
        records.append(
            DashboardRecord(
                timestamp=int.from_bytes(record[0:2], "little"),
                gyro_x=int.from_bytes(record[2:3], "little", signed=True),
                gyro_y=int.from_bytes(record[3:4], "little", signed=True),
                gyro_z=int.from_bytes(record[4:5], "little", signed=True),
                motion_x=int.from_bytes(record[5:6], "little", signed=True),
                motion_y=int.from_bytes(record[6:7], "little", signed=True),
                motion_z=int.from_bytes(record[7:8], "little", signed=True),
            )
        )
    return tuple(records)


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
        milliamperes = int.from_bytes(payload[5:7], "little", signed=True)
        if milliamperes != -1:
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


def derive_pacer_progress(
    elapsed_seconds: int, sector_times: list[int]
) -> tuple[int | None, int | None]:
    """Return the pacer sector and elapsed seconds within that sector."""
    if elapsed_seconds < 0 or not sector_times:
        return None, None

    elapsed = elapsed_seconds
    for sector, duration in enumerate(sector_times, start=1):
        if elapsed < duration:
            return sector, elapsed
        if sector < len(sector_times):
            elapsed -= duration

    # The final sector remains active when brushing continues past the target.
    return len(sector_times), elapsed


def advance_pacer_progress(
    sector: int,
    sector_timer: int,
    elapsed_delta: int,
    sector_times: list[int],
) -> tuple[int | None, int | None]:
    """Advance an authoritative pacer sample without another BLE read."""
    if (
        sector < 1
        or sector > len(sector_times)
        or sector_timer < 0
        or elapsed_delta < 0
        or not sector_times
    ):
        return None, None

    timer = sector_timer + elapsed_delta
    while sector < len(sector_times) and timer >= sector_times[sector - 1]:
        timer -= sector_times[sector - 1]
        sector += 1
    return sector, timer


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


def advance_session_timer_evidence(
    baseline: int | None, seconds: int
) -> tuple[int, bool]:
    """Track an authoritative timer and report genuine forward progress.

    A first sample can be the previous retained timer. A lower sample means the
    timer reset for a new session; only a later higher value confirms brushing.
    """
    if baseline is None or seconds < baseline:
        return seconds, False
    return baseline, seconds > baseline


def parse_session_record(payload: bytes | bytearray) -> dict[str, int | float]:
    """Decode the protocol 7/8 FF29 retained-session summary.

    The two 16-bit words at offsets 4 and 6 contain packed identifiers and
    configuration. Pressure times use 100 ms units; pressure magnitudes use
    100 mN units. The layout is verified against the vendor parser and a real
    offline session recovered through an iO Sense charger.
    """
    if len(payload) < 20:
        return {}
    packed_identity = int.from_bytes(payload[4:6], "little")
    packed_configuration = int.from_bytes(payload[6:8], "little")
    high_pressure_time = int.from_bytes(payload[10:12], "little")
    low_pressure_time = int.from_bytes(payload[12:14], "little")
    result: dict[str, int | float] = {
        "session_timestamp": int.from_bytes(payload[0:4], "little"),
        "session_id": packed_identity & 0x1FFF,
        "user_id": (packed_identity & 0xE000) >> 13,
        "target_duration": packed_configuration & 0x1FFF,
        "number_of_sectors": (packed_configuration & 0xE000) >> 13,
        "duration": int.from_bytes(payload[8:10], "little"),
        "high_pressure_time": high_pressure_time / 10,
        "low_pressure_time": low_pressure_time / 10,
        "average_pressure": payload[14] * 100,
        "maximum_pressure": payload[15] * 100,
        "high_pressure_events": payload[16],
        "low_pressure_events": payload[17],
        "on_events": payload[18],
        "mode_raw": payload[19],
    }
    if len(payload) >= 21 and payload[20] <= 100:
        result["battery_end"] = payload[20]
    return result


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


def decode_display_face(raw: int) -> int:
    """Decode the three-bit display face carried beside the sector value."""
    return (raw & 0x38) >> 3


def decode_charger_sector(
    raw: int, total: int | None, configured_total: int | None
) -> tuple[str, int | None, int | None]:
    """Decode the zero-based FF09 pacer sector returned by charger passthrough."""
    decoded_total = total if total and 1 <= total <= 8 else configured_total
    if raw == 0xF0:
        return "no_sector", None, decoded_total
    if raw == 0xFF:
        quadrant = decoded_total or 4
    else:
        quadrant = (raw & 0x07) + 1
    return f"sector_{quadrant}", quadrant, decoded_total
