"""Motion-window preparation and mouth-position result mapping.

The BLE and signal-processing pieces in this module are independent from the
temporary validation model. A distributable model can replace the backend
without changing the charger bridge or Home Assistant entities.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Protocol

from .protocol import DashboardRecord

WINDOW_SIZE = 26
FEATURE_COUNT = 6

MOTION_SCALE = 0.03137255
GYRO_SCALE = 4.48
FEATURE_MEANS = (0.48881329, 0.0, -0.02702209, 0.0, -1.72588759, 0.0)
FEATURE_STD_DEVIATIONS = (
    0.31810252,
    0.56983057,
    0.59904806,
    67.53917287,
    22.98771858,
    27.34702038,
)

COMINO_SAMPLE_PERIOD_MS = 40
COMINO_MAX_RESAMPLE_GAP_MS = 2000

COMINO_ZONES = (
    "out_of_mouth",
    "top_right_outside",
    "top_right_onside",
    "bottom_right_onside",
    "top_right_inside",
    "top_left_outside",
    "top_left_onside",
    "bottom_left_onside",
    "top_left_inside",
    "top_center_outside",
    "bottom_center_outside",
    "top_center_inside",
    "bottom_center_inside",
    "bottom_right_outside",
    "bottom_right_inside",
    "bottom_left_outside",
    "bottom_left_inside",
    "center_outside",
    "right_outside",
    "left_outside",
)

# Six anatomical regions in the conventional card/display order:
# lower-left, lower-front, lower-right, upper-right, upper-front, upper-left.
# These feed only the separate research Mouth sector entity; the public
# ``sector`` compatibility key remains the brush's FF09 timed pacer.
ZONE_TO_SECTOR = {
    "bottom_left_onside": 1,
    "bottom_left_outside": 1,
    "bottom_left_inside": 1,
    "bottom_center_outside": 2,
    "bottom_center_inside": 2,
    "bottom_right_onside": 3,
    "bottom_right_outside": 3,
    "bottom_right_inside": 3,
    "top_right_outside": 4,
    "top_right_onside": 4,
    "top_right_inside": 4,
    "top_center_outside": 5,
    "top_center_inside": 5,
    "top_left_outside": 6,
    "top_left_onside": 6,
    "top_left_inside": 6,
}


class PositionModel(Protocol):
    """Stateful model backend consumed by the streaming classifier."""

    def reset(self) -> None:
        """Restore the model's initial recurrent state."""

    def predict(self, window: Sequence[Sequence[float]]) -> Sequence[Sequence[float]]:
        """Return one 20-zone probability vector for each input sample."""


@dataclass(frozen=True)
class PositionResult:
    """Collapsed result for one 26-sample inference window."""

    position: str
    sector: str
    confidence: float
    sample_votes: tuple[int, ...]


class CominoSnapshotResampler:
    """Restore the 25 Hz timeline between charger-forwarded FF0D snapshots."""

    def __init__(self) -> None:
        self._last: DashboardRecord | None = None

    def reset(self) -> None:
        self._last = None

    def add_snapshot(
        self, records: Sequence[DashboardRecord]
    ) -> tuple[DashboardRecord, ...]:
        if len(records) != 2:
            raise ValueError("expected two Comino snapshot records")

        # FF0D returns newest first; inference consumes chronological samples.
        older, newer = records[1], records[0]
        last = self._last
        self._last = newer
        if last is None:
            return (older, newer)

        gap = (older.timestamp - last.timestamp) & 0xFFFF
        if gap == 0:
            return (newer,) if newer.timestamp != last.timestamp else ()
        if gap > COMINO_MAX_RESAMPLE_GAP_MS:
            # A stale/restarted/wrapped stream must not be joined by a long
            # fabricated segment. Seed the next window from the real pair.
            return (older, newer)

        steps = max(1, round(gap / COMINO_SAMPLE_PERIOD_MS))
        interpolated = [
            _interpolate_dashboard_record(last, older, index / steps, gap)
            for index in range(1, steps + 1)
        ]
        if newer.timestamp != older.timestamp:
            interpolated.append(newer)
        return tuple(interpolated)


def _interpolate_dashboard_record(
    start: DashboardRecord,
    end: DashboardRecord,
    fraction: float,
    timestamp_gap: int,
) -> DashboardRecord:
    def _axis(name: str) -> int:
        start_value = getattr(start, name)
        return round(start_value + (getattr(end, name) - start_value) * fraction)

    return DashboardRecord(
        timestamp=(start.timestamp + round(timestamp_gap * fraction)) & 0xFFFF,
        gyro_x=_axis("gyro_x"),
        gyro_y=_axis("gyro_y"),
        gyro_z=_axis("gyro_z"),
        motion_x=_axis("motion_x"),
        motion_y=_axis("motion_y"),
        motion_z=_axis("motion_z"),
    )


def normalize_dashboard_record(record: DashboardRecord) -> tuple[float, ...]:
    """Calibrate and normalize one record exactly like the live app path."""
    calibrated = (
        record.motion_x * MOTION_SCALE,
        record.motion_y * MOTION_SCALE,
        record.motion_z * MOTION_SCALE,
        record.gyro_x * GYRO_SCALE,
        record.gyro_y * GYRO_SCALE,
        record.gyro_z * GYRO_SCALE,
    )
    return tuple(
        (value - mean) / deviation
        for value, mean, deviation in zip(
            calibrated, FEATURE_MEANS, FEATURE_STD_DEVIATIONS
        )
    )


def collapse_position_probabilities(
    probabilities: Sequence[Sequence[float]],
) -> PositionResult:
    """Apply the app's per-sample argmax and 26-sample majority vote."""
    if len(probabilities) != WINDOW_SIZE:
        raise ValueError(f"expected {WINDOW_SIZE} probability vectors")
    votes = []
    for vector in probabilities:
        if len(vector) != len(COMINO_ZONES):
            raise ValueError(f"expected {len(COMINO_ZONES)} zone probabilities")
        votes.append(max(range(len(vector)), key=lambda index: vector[index]))

    counts = Counter(votes)
    # max() over ascending indices reproduces the app's lowest-index tie break.
    winner = max(range(len(COMINO_ZONES)), key=lambda index: (counts[index], -index))
    position = COMINO_ZONES[winner]
    sector_number = ZONE_TO_SECTOR.get(position)
    sector = f"sector_{sector_number}" if sector_number else "no_sector"
    return PositionResult(
        position=position,
        sector=sector,
        confidence=counts[winner] / WINDOW_SIZE,
        sample_votes=tuple(votes),
    )


class StreamingPositionClassifier:
    """Collect motion samples and classify one non-overlapping window per second."""

    def __init__(self, model: PositionModel) -> None:
        self.model = model
        self._window: list[tuple[float, ...]] = []

    def reset(self) -> None:
        self._window.clear()
        self.model.reset()

    def add_records(self, records: Sequence[DashboardRecord]) -> list[PositionResult]:
        results = []
        for record in records:
            self._window.append(normalize_dashboard_record(record))
            if len(self._window) == WINDOW_SIZE:
                results.append(
                    collapse_position_probabilities(self.model.predict(self._window))
                )
                self._window.clear()
        return results
