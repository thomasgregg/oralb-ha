"""Regression tests for passively tracked session durations."""

from __future__ import annotations

import asyncio
import importlib
import pathlib
import sys
import types
import unittest
from unittest.mock import MagicMock

COMPONENT_PATH = (
    pathlib.Path(__file__).parents[1] / "custom_components" / "oralb_live"
)

# Import the modules without executing the integration's __init__.py, which
# would pull in the whole Home Assistant setup stack for a decoder test.
_PACKAGE = types.ModuleType("oralb_live")
_PACKAGE.__path__ = [str(COMPONENT_PATH)]
sys.modules.setdefault("oralb_live", _PACKAGE)

try:
    const = importlib.import_module("oralb_live.const")
    coordinator = importlib.import_module("oralb_live.coordinator")
except ImportError as err:  # pragma: no cover - environment without HA
    raise unittest.SkipTest(f"Home Assistant is not importable: {err}") from err


def _advertisement(payload: bytes):
    """A minimal stand-in for what the bluetooth callback hands over."""
    return types.SimpleNamespace(
        manufacturer_data={const.ORALB_MANUFACTURER_ID: payload},
        rssi=-60,
        address="AA:BB:CC:DD:EE:FF",
    )


def _payload(state: int, seconds: int, sector: int = 1) -> bytes:
    """Build a protocol 6 advertisement carrying a state and a timer."""
    return bytes(
        [
            0x06,  # protocol
            0x31,  # model type
            0x32,  # firmware
            state,
            0x72,  # pressure
            seconds // 256,
            seconds % 256,
            0x00,  # mode
            sector,
            0x00,  # total sectors
            0x00,  # sector timer
        ]
    )


class PassiveSessionDurationTests(unittest.TestCase):
    """The recorded duration has to match what the brush displayed."""

    def _coordinator(self):
        coordinator.async_dispatcher_send = lambda *args, **kwargs: None
        c = coordinator.OralBLiveCoordinator(
            MagicMock(), "AA:BB:CC:DD:EE:FF", "test", const.CONNECTION_MODE_CHARGER
        )
        c._maybe_schedule_sync = lambda *args, **kwargs: None
        c._schedule_connect = lambda *args, **kwargs: None
        return c

    def test_final_timer_of_the_ending_advertisement_is_recorded(self) -> None:
        """The quiet advertisement carries the session's highest timer value.

        A brush advertises roughly every ten seconds, and the value it sends
        alongside idle is the one it stopped at. Losing it leaves the record
        up to a full advertising interval short of the reported time.
        """
        c = self._coordinator()
        for seconds in (5, 60, 123):
            c._parse_advertisement(_advertisement(_payload(3, seconds)))
        c._parse_advertisement(_advertisement(_payload(2, 130)))

        self.assertEqual(c.data["time"], 130)
        self.assertEqual(c.data["last_session_duration"], 130)

    def test_first_running_advertisement_still_starts_the_session(self) -> None:
        """A session seen for a single advertisement keeps that timer."""
        c = self._coordinator()
        c._parse_advertisement(_advertisement(_payload(3, 42)))
        c._parse_advertisement(_advertisement(_payload(2, 42)))

        self.assertEqual(c.data["last_session_duration"], 42)

    def test_a_quiet_brush_does_not_invent_a_session(self) -> None:
        """Idle advertisements on their own record nothing."""
        c = self._coordinator()
        for _ in range(3):
            c._parse_advertisement(_advertisement(_payload(2, 0)))

        self.assertIsNone(c.data["last_session_duration"])

    def test_invalid_session_record_does_not_overwrite_battery(self) -> None:
        """An uncommitted all-zero FF29 buffer is not a battery reading."""
        c = self._coordinator()
        c.data["battery"] = 76
        c.data["battery_updated_at"] = "previous-update"
        c.data["battery_source"] = const.DATA_SOURCE_DIRECT

        result = asyncio.run(c._async_apply_session_record(bytes(21), None))

        self.assertEqual(result, "invalid")
        self.assertEqual(c.data["battery"], 76)
        self.assertEqual(c.data["battery_updated_at"], "previous-update")
        self.assertEqual(c.data["battery_source"], const.DATA_SOURCE_DIRECT)

    def test_valid_duplicate_session_record_refreshes_battery(self) -> None:
        """A previously counted real record remains useful for battery state."""
        c = self._coordinator()
        record = bytes.fromhex("26e4ff3161017800800064000a001321280201045e")
        c._last_synced_session_ts = int.from_bytes(record[0:4], "little")

        result = asyncio.run(c._async_apply_session_record(record, None))

        self.assertEqual(result, "duplicate")
        self.assertEqual(c.data["battery"], 94)
        self.assertIsNotNone(c.data["battery_updated_at"])
        self.assertEqual(c.data["battery_source"], const.DATA_SOURCE_SESSION)


if __name__ == "__main__":
    unittest.main()
