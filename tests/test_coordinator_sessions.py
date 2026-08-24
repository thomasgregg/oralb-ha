"""Regression tests for passively tracked session durations."""

from __future__ import annotations

import asyncio
import importlib
import pathlib
import sys
import types
import unittest
from datetime import timedelta
from unittest.mock import AsyncMock, MagicMock, patch

COMPONENT_PATH = (
    pathlib.Path(__file__).parents[1] / "custom_components" / "oralb_live"
)

# Import the modules without executing the integration's __init__.py, which
# would pull in the whole Home Assistant setup stack for a decoder test.
_PACKAGE = types.ModuleType("oralb_live")
_PACKAGE.__path__ = [str(COMPONENT_PATH)]
sys.modules.setdefault("oralb_live", _PACKAGE)

# These imports intentionally fail collection when the Home Assistant test
# dependencies are absent. Silently skipping this module would hide the
# integration's coordinator, session and restore regressions behind a green
# test run.
const = importlib.import_module("oralb_live.const")
coordinator = importlib.import_module("oralb_live.coordinator")
sensor = importlib.import_module("oralb_live.sensor")


def _advertisement(payload: bytes):
    """A minimal stand-in for what the bluetooth callback hands over."""
    return types.SimpleNamespace(
        manufacturer_data={const.ORALB_MANUFACTURER_ID: payload},
        rssi=-60,
        address="AA:BB:CC:DD:EE:FF",
    )


def _payload(
    state: int,
    seconds: int,
    sector: int = 1,
    face: int = 0,
    pressure: int = 0x72,
) -> bytes:
    """Build a protocol 6 advertisement carrying a state and a timer."""
    return bytes(
        [
            0x06,  # protocol
            0x31,  # model type
            0x32,  # firmware
            state,
            pressure,  # pressure/status flags
            seconds // 256,
            seconds % 256,
            0x00,  # mode
            sector | (face << 3),
            0x00,  # total sectors
            0x00,  # sector timer
        ]
    )


class PassiveSessionDurationTests(unittest.TestCase):
    """The recorded duration has to match what the brush displayed."""

    def _coordinator(self, mode: str = const.CONNECTION_MODE_CHARGER):
        coordinator.async_dispatcher_send = lambda *args, **kwargs: None
        c = coordinator.OralBLiveCoordinator(
            MagicMock(), "AA:BB:CC:DD:EE:FF", "test", mode
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

    def test_pressure_sample_exposes_raw_motor_diagnostics(self) -> None:
        c = self._coordinator()
        c._apply_state(const.RUNNING_STATE)
        c._apply_pressure(
            bytes.fromhex("01 34 12 78 56 bc 9a f0 de aa"),
            const.DATA_SOURCE_CHARGER,
        )

        self.assertEqual(c.data["pressure"], "normal")
        self.assertEqual(c.data["pressure_force"], 0x5678)
        self.assertEqual(c.data["motor_angle_raw"], 0x9ABC)
        self.assertEqual(c.data["motor_target_raw"], 0xDEF0)

        c._apply_pressure(bytes([0]), const.DATA_SOURCE_DIRECT)

        self.assertEqual(c.data["pressure"], "low")
        self.assertIsNone(c.data["pressure_force"])
        self.assertIsNone(c.data["motor_angle_raw"])
        self.assertIsNone(c.data["motor_target_raw"])

    def test_idle_pressure_is_unknown_across_source_changes(self) -> None:
        """Reconnects cannot turn an untouched handle into a pressure reading."""
        c = self._coordinator()

        c._parse_advertisement(_advertisement(_payload(2, 0, pressure=0x72)))
        self.assertIsNone(c.data["pressure"])
        self.assertIsNone(c.data["pressure_raw"])
        self.assertIsNone(c.data["pressure_source"])

        c._apply_pressure(bytes([0]), const.DATA_SOURCE_DIRECT)
        self.assertIsNone(c.data["pressure"])
        self.assertIsNone(c.data["pressure_raw"])
        self.assertIsNone(c.data["pressure_source"])

        c._parse_advertisement(_advertisement(_payload(2, 0, pressure=0x52)))
        self.assertIsNone(c.data["pressure"])
        self.assertIsNone(c.data["pressure_raw"])
        self.assertIsNone(c.data["pressure_source"])
        self.assertIsNone(c.data["last_session_duration"])

    def test_active_advertisement_pressure_uses_status_high_bit(self) -> None:
        """Normal, button-only and high-bit advertisement values stay distinct."""
        c = self._coordinator()

        c._parse_advertisement(_advertisement(_payload(3, 1, pressure=0x72)))
        self.assertEqual(c.data["pressure"], "normal")
        self.assertEqual(c.data["pressure_raw"], "72")
        self.assertEqual(c.data["pressure_source"], const.DATA_SOURCE_ADVERTISEMENT)

        c._parse_advertisement(_advertisement(_payload(3, 2, pressure=0x38)))
        self.assertEqual(c.data["pressure"], "normal")

        c._parse_advertisement(_advertisement(_payload(3, 3, pressure=0xF2)))
        self.assertEqual(c.data["pressure"], "high")

    def test_ending_session_clears_pressure_after_accounting(self) -> None:
        """The quiet edge clears live pressure without inventing a low event."""
        c = self._coordinator()
        c._parse_advertisement(_advertisement(_payload(3, 20, pressure=0x72)))
        c._parse_advertisement(_advertisement(_payload(2, 21, pressure=0x72)))

        self.assertIsNone(c.data["pressure"])
        self.assertIsNone(c.data["pressure_source"])
        self.assertEqual(c.data["last_session_low_pressure"], 0)
        self.assertEqual(c.data["last_session_high_pressure"], 0)

    def test_cached_advertisement_does_not_restore_transient_pressure(self) -> None:
        """A startup cache may seed identity but never live contact feedback."""
        c = self._coordinator()
        c._parse_advertisement(
            _advertisement(_payload(3, 20, pressure=0xF2)), track_session=False
        )

        self.assertIsNone(c.data["pressure"])
        self.assertIsNone(c.data["pressure_raw"])
        self.assertIsNone(c.data["pressure_source"])

    def test_selection_menu_can_carry_passive_session_pressure(self) -> None:
        """Firmware using selection_menu for brushing keeps pressure active."""
        c = self._coordinator()
        c._parse_advertisement(_advertisement(_payload(8, 0, pressure=0xF2)))

        self.assertEqual(c.data["pressure"], "high")
        self.assertEqual(c.data["pressure_source"], const.DATA_SOURCE_ADVERTISEMENT)

    def test_direct_mode_requires_running_for_pressure(self) -> None:
        """Direct selection_menu is not the passive firmware brushing quirk."""
        c = self._coordinator(const.CONNECTION_MODE_LIVE)

        c._parse_advertisement(_advertisement(_payload(8, 0, pressure=0xF2)))
        self.assertIsNone(c.data["pressure"])

        c._parse_advertisement(_advertisement(_payload(3, 1, pressure=0xF2)))
        self.assertEqual(c.data["pressure"], "high")

        for raw, expected in ((0, "low"), (1, "normal"), (2, "high")):
            with self.subTest(raw=raw):
                c._apply_pressure(bytes([raw]), const.DATA_SOURCE_DIRECT)
                self.assertEqual(c.data["pressure"], expected)
                self.assertEqual(c.data["pressure_source"], const.DATA_SOURCE_DIRECT)

        c._apply_state(2)
        self.assertIsNone(c.data["pressure"])
        self.assertIsNone(c.data["pressure_source"])

    def test_ending_advertisement_keeps_face_with_session(self) -> None:
        """The result face belongs to the session that just ended."""
        c = self._coordinator()
        c._parse_advertisement(_advertisement(_payload(3, 60)))
        c._parse_advertisement(_advertisement(_payload(2, 69, face=5)))

        self.assertEqual(c.data["smiley"], "special_5")
        self.assertEqual(c.data["last_session_display_face"], "special_5")

    def test_ending_advertisement_without_result_keeps_face_unknown(self) -> None:
        """Display-off is not a substitute for a missing session verdict."""
        c = self._coordinator()
        c._parse_advertisement(_advertisement(_payload(3, 60)))
        c._parse_advertisement(_advertisement(_payload(2, 69, face=0)))

        self.assertEqual(c.data["smiley"], "off")
        self.assertIsNone(c.data["last_session_display_face"])

    def test_ending_advertisement_keeps_standard_result_with_session(self) -> None:
        """Raw face 1 is the lowest verdict, not an unavailable placeholder."""
        c = self._coordinator()
        c._parse_advertisement(_advertisement(_payload(3, 20, face=0)))
        c._parse_advertisement(_advertisement(_payload(2, 21, face=1)))

        self.assertEqual(c.data["smiley"], "standard")
        self.assertEqual(c.data["last_session_display_face"], "standard")

    def test_result_on_next_quiet_advertisement_is_still_captured(self) -> None:
        """The result may appear one packet after the state-ending packet."""
        c = self._coordinator()
        c._parse_advertisement(_advertisement(_payload(3, 60)))
        c._parse_advertisement(_advertisement(_payload(2, 69, face=0)))
        c._parse_advertisement(_advertisement(_payload(2, 69, face=5)))

        self.assertEqual(c.data["last_session_display_face"], "special_5")

    def test_later_live_face_does_not_overwrite_session_result(self) -> None:
        """Waking the selection menu changes Smiley, not Last session."""
        c = self._coordinator()
        c._parse_advertisement(_advertisement(_payload(3, 60)))
        c._parse_advertisement(_advertisement(_payload(2, 69, face=5)))

        c._parse_advertisement(_advertisement(_payload(8, 0, face=0)))
        c._parse_advertisement(_advertisement(_payload(2, 0, face=0)))

        self.assertEqual(c.data["smiley"], "off")
        self.assertEqual(c.data["last_session_display_face"], "special_5")

    def test_ignored_provisional_session_does_not_replace_result_face(self) -> None:
        """A menu-only transition cannot attach its face to the last session."""
        c = self._coordinator()
        c._parse_advertisement(_advertisement(_payload(3, 30)))
        c._parse_advertisement(_advertisement(_payload(2, 30, face=4)))

        c._parse_advertisement(_advertisement(_payload(8, 0, face=2)))
        c._parse_advertisement(_advertisement(_payload(4, 0, face=2)))

        self.assertEqual(c.data["last_session_display_face"], "special_4")

    def test_back_to_back_sessions_keep_only_the_latest_result_face(self) -> None:
        """A newly completed session replaces the previous session's face."""
        c = self._coordinator()
        c._parse_advertisement(_advertisement(_payload(3, 30)))
        c._parse_advertisement(_advertisement(_payload(2, 30, face=4)))
        c._parse_advertisement(_advertisement(_payload(3, 45)))
        c._parse_advertisement(_advertisement(_payload(2, 45, face=7)))

        self.assertEqual(c.data["last_session_display_face"], "special_7")

    def test_back_to_back_session_without_result_does_not_inherit_face(self) -> None:
        """The next passive session stays unknown when it has no verdict."""
        c = self._coordinator()
        c._parse_advertisement(_advertisement(_payload(3, 30)))
        c._parse_advertisement(_advertisement(_payload(2, 30, face=4)))
        c._parse_advertisement(_advertisement(_payload(3, 45)))
        c._parse_advertisement(_advertisement(_payload(2, 45, face=0)))

        self.assertIsNone(c.data["last_session_display_face"])

    def test_new_session_without_correlated_face_clears_previous_result(self) -> None:
        """A direct session must not inherit an older advertisement result."""
        coordinator.async_dispatcher_send = lambda *args, **kwargs: None
        c = coordinator.OralBLiveCoordinator(
            MagicMock(), "AA:BB:CC:DD:EE:FF", "test", const.CONNECTION_MODE_LIVE
        )
        c.data["last_session_display_face"] = "special_5"

        c._apply_state(const.RUNNING_STATE)
        c._track_session_time(30, confirm_session=True)
        c._apply_state(2)

        self.assertIsNone(c.data["last_session_display_face"])

    def test_a_quiet_brush_does_not_invent_a_session(self) -> None:
        """Idle advertisements on their own record nothing."""
        c = self._coordinator()
        for _ in range(3):
            c._parse_advertisement(_advertisement(_payload(2, 0)))

        self.assertIsNone(c.data["last_session_duration"])

    def test_menu_only_observation_remains_provisional(self) -> None:
        """Opening the mode menu without timer progress is not brushing."""
        c = self._coordinator()
        c._parse_advertisement(_advertisement(_payload(8, 0)))
        c._parse_advertisement(_advertisement(_payload(4, 0)))

        self.assertIsNone(c.data["last_session_duration"])
        self.assertEqual(c._session_generation, 1)
        self.assertTrue(c._session_pending_sync)

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

    def test_retained_record_preserves_face_when_reconciling_same_session(self) -> None:
        """FF29 refines a passive record but has no replacement face."""
        c = self._coordinator()
        c._parse_advertisement(_advertisement(_payload(3, 110)))
        c._parse_advertisement(_advertisement(_payload(2, 120, face=6)))
        passive_start = c.data["last_session_start"]
        record = bytes.fromhex("26e4ff3161017800800064000a001321280201045e")
        c._last_synced_session_ts = 0
        c._store.async_save = AsyncMock()

        result = asyncio.run(
            c._async_apply_session_record(
                record,
                record[:4],
                rtc_sampled_at=passive_start,
            )
        )

        self.assertEqual(result, "new")
        self.assertEqual(c.data["last_session_source"], const.DATA_SOURCE_SESSION)
        self.assertEqual(c.data["last_session_display_face"], "special_6")

    def test_reconciled_record_keeps_window_open_for_late_face(self) -> None:
        """FF29 may arrive before FF0A without losing same-session association."""
        c = self._coordinator()
        c._parse_advertisement(_advertisement(_payload(3, 110)))
        c._parse_advertisement(_advertisement(_payload(2, 120, face=0)))
        passive_start = c.data["last_session_start"]
        record = bytes.fromhex("26e4ff3161017800800064000a001321280201045e")
        c._last_synced_session_ts = 0
        c._store.async_save = AsyncMock()

        result = asyncio.run(
            c._async_apply_session_record(
                record,
                record[:4],
                rtc_sampled_at=passive_start,
            )
        )
        c._apply_smiley(b"\x06", source=const.DATA_SOURCE_CHARGER)

        self.assertEqual(result, "new")
        self.assertEqual(c.data["last_session_display_face"], "special_6")

    def test_new_retained_record_does_not_inherit_previous_face(self) -> None:
        """A newer FF29-only session has an unknown face, not a stale one."""
        c = self._coordinator()
        record = bytes.fromhex("26e4ff3161017800800064000a001321280201045e")
        c._last_synced_session_ts = 0
        c._store.async_save = AsyncMock()
        now = coordinator.dt_util.utcnow()
        c.data["last_session_start"] = now - timedelta(hours=1)
        c.data["last_session_display_face"] = "special_5"

        result = asyncio.run(
            c._async_apply_session_record(
                record,
                record[:4],
                rtc_sampled_at=now,
            )
        )

        self.assertEqual(result, "new")
        self.assertIsNone(c.data["last_session_display_face"])

    def test_new_retained_record_closes_old_face_capture_window(self) -> None:
        """Delayed FF0A for a predecessor cannot modify a newer FF29 record."""
        c = self._coordinator()
        c._parse_advertisement(_advertisement(_payload(3, 60)))
        c._parse_advertisement(_advertisement(_payload(2, 60, face=0)))
        record = bytes.fromhex("26e4ff3161017800800064000a001321280201045e")
        c._last_synced_session_ts = 0
        c._store.async_save = AsyncMock()
        newer_wall_time = c.data["last_session_start"] + timedelta(minutes=10)

        result = asyncio.run(
            c._async_apply_session_record(
                record,
                record[:4],
                rtc_sampled_at=newer_wall_time,
            )
        )
        c._apply_smiley(b"\x06", source=const.DATA_SOURCE_DIRECT)

        self.assertEqual(result, "new")
        self.assertIsNone(c.data["last_session_display_face"])


class ConnectedSessionDisplayFaceRegressionTests(unittest.TestCase):
    """Reproduce display-face loss on connected delivery paths from issue 18."""

    def _coordinator(self, mode: str):
        coordinator.async_dispatcher_send = lambda *args, **kwargs: None
        c = coordinator.OralBLiveCoordinator(
            MagicMock(), "AA:BB:CC:DD:EE:FF", "test", mode
        )
        c._maybe_schedule_sync = lambda *args, **kwargs: None
        c._schedule_connect = lambda *args, **kwargs: None
        c.data["data_source"] = (
            const.DATA_SOURCE_DIRECT
            if mode == const.CONNECTION_MODE_LIVE
            else const.DATA_SOURCE_CHARGER
        )
        return c

    @staticmethod
    def _notify(c, uuid: str, payload: bytes) -> None:
        char = types.SimpleNamespace(uuid=uuid)
        c._on_notify(char, bytearray(payload))

    def _complete_session(self, c) -> None:
        self._notify(c, const.CHAR_STATE, bytes((const.RUNNING_STATE,)))
        self._notify(c, const.CHAR_BRUSH_TIME, b"\x00\x3e")
        self._notify(c, const.CHAR_STATE, b"\x02")

    def test_direct_ff0a_notification_is_attached_to_completed_session(self) -> None:
        """Match the reported special_3 notification at the direct idle edge."""
        c = self._coordinator(const.CONNECTION_MODE_LIVE)

        self._complete_session(c)
        self._notify(c, const.CHAR_SMILEY, b"\x03")

        self.assertEqual(c.data["smiley"], "special_3")
        self.assertEqual(c.data["last_session_display_face"], "special_3")

    def test_direct_session_without_ff0a_remains_unknown(self) -> None:
        """No notification is a normal unavailable result, not a placeholder."""
        c = self._coordinator(const.CONNECTION_MODE_LIVE)
        c._apply_smiley(b"\x00")

        self._complete_session(c)

        self.assertEqual(c.data["smiley"], "off")
        self.assertIsNone(c.data["last_session_display_face"])

    def test_direct_standard_notification_is_attached_to_completed_session(
        self,
    ) -> None:
        """A directly notified raw face 1 is a genuine low-score verdict."""
        c = self._coordinator(const.CONNECTION_MODE_LIVE)

        self._complete_session(c)
        self._notify(c, const.CHAR_SMILEY, b"\x01")

        self.assertEqual(c.data["smiley"], "standard")
        self.assertEqual(c.data["last_session_display_face"], "standard")

    def test_standard_notification_just_before_idle_is_still_correlated(self) -> None:
        """Raw face 1 follows the same cross-character ordering rules as higher faces."""
        c = self._coordinator(const.CONNECTION_MODE_LIVE)

        with patch.object(coordinator.time, "monotonic", return_value=100.0):
            self._notify(c, const.CHAR_STATE, bytes((const.RUNNING_STATE,)))
            self._notify(c, const.CHAR_BRUSH_TIME, b"\x00\x15")
            self._notify(c, const.CHAR_SMILEY, b"\x01")
        with patch.object(coordinator.time, "monotonic", return_value=101.0):
            self._notify(c, const.CHAR_STATE, b"\x02")

        self.assertEqual(c.data["last_session_display_face"], "standard")

    def test_result_notification_just_before_idle_is_still_correlated(self) -> None:
        """FF0A and FF04 ordering cannot decide whether the result is kept."""
        c = self._coordinator(const.CONNECTION_MODE_LIVE)

        with patch.object(coordinator.time, "monotonic", return_value=100.0):
            self._notify(c, const.CHAR_STATE, bytes((const.RUNNING_STATE,)))
            self._notify(c, const.CHAR_BRUSH_TIME, b"\x00\x3e")
            self._notify(c, const.CHAR_SMILEY, b"\x06")
        with patch.object(coordinator.time, "monotonic", return_value=101.0):
            self._notify(c, const.CHAR_STATE, b"\x02")

        self.assertEqual(c.data["last_session_display_face"], "special_6")

    def test_old_in_session_face_is_not_mistaken_for_the_result(self) -> None:
        """Only a sample close to the state edge may cross notification order."""
        c = self._coordinator(const.CONNECTION_MODE_LIVE)

        with patch.object(coordinator.time, "monotonic", return_value=100.0):
            self._notify(c, const.CHAR_STATE, bytes((const.RUNNING_STATE,)))
            self._notify(c, const.CHAR_BRUSH_TIME, b"\x00\x3e")
            self._notify(c, const.CHAR_SMILEY, b"\x06")
        with patch.object(coordinator.time, "monotonic", return_value=103.0):
            self._notify(c, const.CHAR_STATE, b"\x02")

        self.assertIsNone(c.data["last_session_display_face"])

    def test_first_result_is_frozen_against_later_display_changes(self) -> None:
        """A captured verdict cannot become another face or display-off."""
        c = self._coordinator(const.CONNECTION_MODE_LIVE)

        self._complete_session(c)
        self._notify(c, const.CHAR_SMILEY, b"\x03")
        self._notify(c, const.CHAR_SMILEY, b"\x04")
        self._notify(c, const.CHAR_SMILEY, b"\x00")

        self.assertEqual(c.data["smiley"], "off")
        self.assertEqual(c.data["last_session_display_face"], "special_3")

    def test_standard_result_is_frozen_against_later_display_changes(self) -> None:
        """The lowest verdict has the same immutability as every higher result."""
        c = self._coordinator(const.CONNECTION_MODE_LIVE)

        self._complete_session(c)
        self._notify(c, const.CHAR_SMILEY, b"\x01")
        self._notify(c, const.CHAR_SMILEY, b"\x03")
        self._notify(c, const.CHAR_SMILEY, b"\x00")

        self.assertEqual(c.data["smiley"], "off")
        self.assertEqual(c.data["last_session_display_face"], "standard")

    def test_result_after_capture_window_is_not_attached(self) -> None:
        """A later menu face must not leak into the completed session."""
        c = self._coordinator(const.CONNECTION_MODE_LIVE)

        with patch.object(coordinator.time, "monotonic", return_value=100.0):
            self._complete_session(c)
        with patch.object(
            coordinator.time,
            "monotonic",
            return_value=(
                100.0 + const.SESSION_DISPLAY_FACE_CAPTURE_WINDOW_SECONDS + 0.1
            ),
        ):
            self._notify(c, const.CHAR_SMILEY, b"\x05")

        self.assertEqual(c.data["smiley"], "special_5")
        self.assertIsNone(c.data["last_session_display_face"])

    def test_standard_after_capture_window_is_not_attached(self) -> None:
        """Accepting raw face 1 must not weaken the bounded-session guard."""
        c = self._coordinator(const.CONNECTION_MODE_LIVE)

        with patch.object(coordinator.time, "monotonic", return_value=100.0):
            self._complete_session(c)
        with patch.object(
            coordinator.time,
            "monotonic",
            return_value=(
                100.0 + const.SESSION_DISPLAY_FACE_CAPTURE_WINDOW_SECONDS + 0.1
            ),
        ):
            self._notify(c, const.CHAR_SMILEY, b"\x01")

        self.assertEqual(c.data["smiley"], "standard")
        self.assertIsNone(c.data["last_session_display_face"])

    def test_new_session_invalidates_previous_capture_window(self) -> None:
        """A delayed result from a predecessor cannot attach after a new start."""
        c = self._coordinator(const.CONNECTION_MODE_LIVE)

        self._complete_session(c)
        self._notify(c, const.CHAR_STATE, bytes((const.RUNNING_STATE,)))
        self._notify(c, const.CHAR_SMILEY, b"\x05")

        self.assertIsNone(c.data["last_session_display_face"])

    def test_charger_forwarded_ff0a_is_attached_to_completed_session(self) -> None:
        """The iO Sense FF0A path must associate the same way as direct GATT."""
        c = self._coordinator(const.CONNECTION_MODE_CHARGER)
        c._apply_state(const.RUNNING_STATE)
        c._track_session_time(62, confirm_session=True)
        c._apply_state(2)

        asyncio.run(c._async_apply_charger_passthrough("FF0A", b"\x04"))

        self.assertEqual(c.data["smiley"], "special_4")
        self.assertEqual(c.data["last_session_display_face"], "special_4")

    def test_charger_forwarded_standard_is_attached_to_completed_session(self) -> None:
        """The iO Sense path must retain raw face 1 as a valid verdict."""
        c = self._coordinator(const.CONNECTION_MODE_CHARGER)
        c._apply_state(const.RUNNING_STATE)
        c._track_session_time(21, confirm_session=True)
        c._apply_state(2)

        asyncio.run(c._async_apply_charger_passthrough("FF0A", b"\x01"))

        self.assertEqual(c.data["smiley"], "standard")
        self.assertEqual(c.data["last_session_display_face"], "standard")


class ConnectedSessionDisplayFaceReadTests(unittest.IsolatedAsyncioTestCase):
    """Verify the best-effort direct read fallback and its safety bounds."""

    def _coordinator(self):
        coordinator.async_dispatcher_send = lambda *args, **kwargs: None
        tasks: list[asyncio.Task] = []
        hass = MagicMock()

        def _create_background_task(coro, name):
            task = asyncio.create_task(coro, name=name)
            tasks.append(task)
            return task

        hass.async_create_background_task.side_effect = _create_background_task
        c = coordinator.OralBLiveCoordinator(
            hass,
            "AA:BB:CC:DD:EE:FF",
            "test",
            const.CONNECTION_MODE_LIVE,
        )
        c.data["data_source"] = const.DATA_SOURCE_DIRECT
        return c, tasks

    @staticmethod
    def _notify(c, uuid: str, payload: bytes) -> None:
        char = types.SimpleNamespace(uuid=uuid)
        c._on_notify(char, bytearray(payload))

    def _complete_session(self, c) -> None:
        self._notify(c, const.CHAR_STATE, bytes((const.RUNNING_STATE,)))
        self._notify(c, const.CHAR_BRUSH_TIME, b"\x00\x3e")
        self._notify(c, const.CHAR_STATE, b"\x02")

    async def test_reads_retry_from_off_to_result_without_disconnect(self) -> None:
        """Reproduce missing notifications and recover the transient by FF0A read."""
        c, tasks = self._coordinator()
        client = MagicMock()
        client.is_connected = True
        client.read_gatt_char = AsyncMock(side_effect=(b"\x00", b"\x03"))
        client.disconnect = AsyncMock()
        c._client = client

        with patch.object(
            coordinator, "SESSION_DISPLAY_FACE_READ_RETRY_DELAYS", (0.0, 0.0, 0.0)
        ):
            self._complete_session(c)
            await tasks[-1]

        self.assertEqual(client.read_gatt_char.await_count, 2)
        self.assertEqual(c.data["smiley"], "special_3")
        self.assertEqual(c.data["last_session_display_face"], "special_3")
        client.disconnect.assert_not_awaited()

    async def test_reads_retry_from_off_to_standard_result(self) -> None:
        """A later raw face 1 read ends retries and supplies the session verdict."""
        c, tasks = self._coordinator()
        client = MagicMock()
        client.is_connected = True
        client.read_gatt_char = AsyncMock(
            side_effect=(coordinator.BleakError("missed"), b"\x00", b"\x01")
        )
        client.disconnect = AsyncMock()
        c._client = client

        with patch.object(
            coordinator, "SESSION_DISPLAY_FACE_READ_RETRY_DELAYS", (0.0, 0.0, 0.0)
        ):
            self._complete_session(c)
            await tasks[-1]

        self.assertEqual(client.read_gatt_char.await_count, 3)
        self.assertEqual(c.data["smiley"], "standard")
        self.assertEqual(c.data["last_session_display_face"], "standard")
        client.disconnect.assert_not_awaited()

    async def test_failed_and_off_reads_leave_literal_null(self) -> None:
        """Errors and display-off still mean honest result unavailability."""
        c, tasks = self._coordinator()
        client = MagicMock()
        client.is_connected = True
        client.read_gatt_char = AsyncMock(
            side_effect=(coordinator.BleakError("missed"), b"\x00", b"\x00")
        )
        client.disconnect = AsyncMock()
        c._client = client

        with patch.object(
            coordinator, "SESSION_DISPLAY_FACE_READ_RETRY_DELAYS", (0.0, 0.0, 0.0)
        ):
            self._complete_session(c)
            await tasks[-1]

        self.assertEqual(client.read_gatt_char.await_count, 3)
        self.assertEqual(c.data["smiley"], "off")
        self.assertIsNone(c.data["last_session_display_face"])
        client.disconnect.assert_not_awaited()

    async def test_notification_closes_capture_before_fallback_reads(self) -> None:
        """A delivered FF0A notification makes active reads unnecessary."""
        c, tasks = self._coordinator()
        client = MagicMock()
        client.is_connected = True
        client.read_gatt_char = AsyncMock(return_value=b"\x07")
        c._client = client

        with patch.object(
            coordinator, "SESSION_DISPLAY_FACE_READ_RETRY_DELAYS", (0.0,)
        ):
            self._complete_session(c)
            self._notify(c, const.CHAR_SMILEY, b"\x03")
            await tasks[-1]

        client.read_gatt_char.assert_not_awaited()
        self.assertEqual(c.data["last_session_display_face"], "special_3")

    async def test_new_session_cancels_predecessor_read_sequence(self) -> None:
        """Back-to-back sessions cannot leave an old retry task running."""
        c, tasks = self._coordinator()
        client = MagicMock()
        client.is_connected = True
        client.read_gatt_char = AsyncMock(return_value=b"\x07")
        c._client = client

        with patch.object(
            coordinator, "SESSION_DISPLAY_FACE_READ_RETRY_DELAYS", (60.0,)
        ):
            self._complete_session(c)
            old_task = tasks[-1]
            self._notify(c, const.CHAR_STATE, bytes((const.RUNNING_STATE,)))
            with self.assertRaises(asyncio.CancelledError):
                await old_task

        client.read_gatt_char.assert_not_awaited()
        self.assertIsNone(c.data["last_session_display_face"])

    async def test_stop_cancels_pending_read_sequence(self) -> None:
        """Integration shutdown leaves no FF0A background task behind."""
        c, tasks = self._coordinator()
        client = MagicMock()
        client.is_connected = True
        client.read_gatt_char = AsyncMock(return_value=b"\x07")
        client.disconnect = AsyncMock()
        c._client = client

        with patch.object(
            coordinator, "SESSION_DISPLAY_FACE_READ_RETRY_DELAYS", (60.0,)
        ):
            self._complete_session(c)
            read_task = tasks[-1]
            await c.async_stop()
            with self.assertRaises(asyncio.CancelledError):
                await read_task

        self.assertTrue(read_task.done())
        client.read_gatt_char.assert_not_awaited()


class SessionSyncRetryTests(unittest.TestCase):
    """Unresolved generations remain recoverable without connection storms."""

    def _coordinator(self):
        coordinator.async_dispatcher_send = lambda *args, **kwargs: None
        c = coordinator.OralBLiveCoordinator(
            MagicMock(), "AA:BB:CC:DD:EE:FF", "test", const.CONNECTION_MODE_CHARGER
        )
        c.data["state_raw"] = 2
        return c

    def _run_sequence(self, c) -> None:
        with (
            patch.object(coordinator, "SESSION_RECORD_SETTLE_SECONDS", 0),
            patch.object(coordinator, "SYNC_RETRY_DELAY_SECONDS", 0),
            patch.object(
                coordinator,
                "SESSION_SYNC_RETRY_BACKOFF_SECONDS",
                (60, 300, 21600),
            ),
            patch.object(coordinator.time, "monotonic", return_value=1000.0),
        ):
            asyncio.run(c._async_sync_sequence())

    def test_failed_generation_stays_pending_and_unprocessed(self) -> None:
        c = self._coordinator()
        c._session_generation = 1
        c._session_pending_sync = True
        c._async_sync_once = AsyncMock(return_value="failed")

        self._run_sequence(c)

        self.assertEqual(c._async_sync_once.await_count, const.SYNC_RETRY_ATTEMPTS)
        self.assertEqual(c._processed_session_generation, 0)
        self.assertTrue(c._session_pending_sync)
        self.assertEqual(c._session_sync_retry_count, 1)
        self.assertEqual(c._session_sync_retry_not_before, 1060.0)

    def test_deferred_retry_uses_one_attempt_and_advances_backoff(self) -> None:
        c = self._coordinator()
        c._session_generation = 1
        c._session_pending_sync = True
        c._session_sync_retry_count = 1
        c._async_sync_once = AsyncMock(return_value="duplicate")

        self._run_sequence(c)

        c._async_sync_once.assert_awaited_once()
        self.assertEqual(c._processed_session_generation, 0)
        self.assertTrue(c._session_pending_sync)
        self.assertEqual(c._session_sync_retry_count, 2)
        self.assertEqual(c._session_sync_retry_not_before, 1300.0)

    def test_retry_backoff_caps_at_periodic_interval(self) -> None:
        c = self._coordinator()
        c._session_generation = 1
        c._session_pending_sync = True
        c._session_sync_retry_count = 3
        c._async_sync_once = AsyncMock(return_value="invalid")

        self._run_sequence(c)

        c._async_sync_once.assert_awaited_once()
        self.assertEqual(c._session_sync_retry_count, 3)
        self.assertEqual(c._session_sync_retry_not_before, 22600.0)

    def test_new_record_resolves_generation_and_retry_state(self) -> None:
        c = self._coordinator()
        c._session_generation = 1
        c._session_pending_sync = True
        c._session_sync_retry_count = 2
        c._session_sync_retry_not_before = 999.0
        c._async_sync_once = AsyncMock(return_value="new")

        self._run_sequence(c)

        c._async_sync_once.assert_awaited_once()
        self.assertEqual(c._processed_session_generation, 1)
        self.assertFalse(c._session_pending_sync)
        self.assertEqual(c._session_sync_retry_count, 0)
        self.assertEqual(c._session_sync_retry_not_before, 0.0)

    def test_concurrent_charger_resolution_does_not_resurrect_pending(self) -> None:
        c = self._coordinator()
        c._session_generation = 1
        c._session_pending_sync = True

        async def _charger_wins():
            c._processed_session_generation = 1
            c._session_pending_sync = False
            return "duplicate"

        c._async_sync_once = AsyncMock(side_effect=_charger_wins)

        self._run_sequence(c)

        c._async_sync_once.assert_awaited_once()
        self.assertEqual(c._processed_session_generation, 1)
        self.assertFalse(c._session_pending_sync)
        self.assertEqual(c._session_sync_retry_count, 0)
        self.assertEqual(c._session_sync_retry_not_before, 0.0)

    def test_newer_generation_is_processed_before_deferring(self) -> None:
        c = self._coordinator()
        c._session_generation = 1
        c._session_pending_sync = True

        async def _new_generation_then_record():
            if c._session_generation == 1:
                c._session_generation = 2
                c._session_sync_retry_count = 0
                c._session_sync_retry_not_before = 0.0
                return "failed"
            return "new"

        c._async_sync_once = AsyncMock(side_effect=_new_generation_then_record)

        self._run_sequence(c)

        self.assertEqual(c._async_sync_once.await_count, 2)
        self.assertEqual(c._processed_session_generation, 2)
        self.assertFalse(c._session_pending_sync)
        self.assertEqual(c._session_sync_retry_count, 0)

    def test_verified_charger_hands_off_without_consuming_retry(self) -> None:
        c = self._coordinator()
        c._session_generation = 1
        c._session_pending_sync = True
        c.charger.address = "11:22:33:44:55:66"
        c._async_sync_once = AsyncMock(return_value="failed")

        self._run_sequence(c)

        c._async_sync_once.assert_not_awaited()
        self.assertEqual(c._processed_session_generation, 0)
        self.assertTrue(c._session_pending_sync)
        self.assertEqual(c._session_sync_retry_count, 0)

    def test_new_generation_resets_deferred_retry(self) -> None:
        c = self._coordinator()
        c._tracked_state_raw = 2
        c._session_sync_retry_count = 3
        c._session_sync_retry_not_before = 999.0

        c._apply_state(8)

        self.assertEqual(c._session_generation, 1)
        self.assertTrue(c._session_pending_sync)
        self.assertEqual(c._session_sync_retry_count, 0)
        self.assertEqual(c._session_sync_retry_not_before, 0.0)

    def test_startup_seed_still_performs_one_probe(self) -> None:
        c = self._coordinator()
        c._session_generation = 0
        c._processed_session_generation = 0
        c._session_pending_sync = True
        c._async_sync_once = AsyncMock(return_value="duplicate")

        self._run_sequence(c)

        c._async_sync_once.assert_awaited_once()
        self.assertFalse(c._session_pending_sync)

    def test_scheduler_honours_deferred_retry_time(self) -> None:
        c = self._coordinator()
        c._session_generation = 1
        c._session_pending_sync = True
        c._last_sync_attempt = 0.0
        c._session_sync_retry_not_before = 200.0

        with patch.object(coordinator.time, "monotonic", return_value=100.0):
            c._maybe_schedule_sync()

        c.hass.async_create_background_task.assert_not_called()


class LiveSectorNumberingTests(unittest.TestCase):
    """FF09 counts its quadrants from zero, the advertisement from one."""

    def _coordinator(self):
        coordinator.async_dispatcher_send = lambda *args, **kwargs: None
        c = coordinator.OralBLiveCoordinator(
            MagicMock(), "AA:BB:CC:DD:EE:FF", "test", const.CONNECTION_MODE_LIVE
        )
        c._maybe_schedule_sync = lambda *args, **kwargs: None
        c._schedule_connect = lambda *args, **kwargs: None
        c.data["state_raw"] = const.RUNNING_STATE
        return c

    def _notify(self, c, quadrant: int, total: int = 4) -> None:
        """Hand the coordinator one FF09 notification."""
        char = types.SimpleNamespace(uuid=const.CHAR_SECTOR)
        c._on_notify(char, bytearray([quadrant, 0, total]))

    def test_the_first_quadrant_is_reported_as_a_sector(self) -> None:
        """Zero means the first zone, not the absence of one.

        Read as an advertisement value it means "no sector defined", which
        is why the opening half minute of every session showed no zone at
        all.
        """
        c = self._coordinator()
        self._notify(c, 0)

        self.assertEqual(c.data["sector"], "sector_1")

    def test_each_quadrant_keeps_its_own_number(self) -> None:
        """A whole four-zone session, one notification per zone."""
        c = self._coordinator()
        for quadrant, expected in enumerate(
            ("sector_1", "sector_2", "sector_3", "sector_4")
        ):
            self._notify(c, quadrant)
            self.assertEqual(c.data["sector"], expected)

    def test_the_advertisement_still_counts_from_one(self) -> None:
        """The other wire format is untouched: its 1 is the first zone."""
        c = self._coordinator()
        c.data["live"] = False
        c._parse_advertisement(_advertisement(_payload(3, 10, sector=1)))

        self.assertEqual(c.data["sector"], "sector_1")


class LastSessionDisplayFaceSensorTests(unittest.IsolatedAsyncioTestCase):
    """The session face is exposed and restored with Last session."""

    def _entity(self):
        coordinator_instance = MagicMock()
        coordinator_instance.address = "AA:BB:CC:DD:EE:FF"
        coordinator_instance.available = True
        coordinator_instance.data = {
            "last_session_start": coordinator.dt_util.utcnow(),
            "last_session_display_face": "special_5",
        }
        description = next(
            item for item in sensor.SENSORS if item.key == "last_session"
        )
        return (
            coordinator_instance,
            sensor.OralBLiveSensor(coordinator_instance, description),
        )

    def test_last_session_exposes_display_face_attribute(self) -> None:
        coordinator_instance, entity = self._entity()

        with patch.object(entity, "async_write_ha_state"):
            entity._handle_update(coordinator_instance.data)

        self.assertEqual(entity.extra_state_attributes["display_face"], "special_5")

    async def test_last_session_restores_display_face_after_restart(self) -> None:
        coordinator_instance, entity = self._entity()
        coordinator_instance.data["last_session_display_face"] = None
        restored = types.SimpleNamespace(
            state=coordinator_instance.data["last_session_start"].isoformat(),
            attributes={"display_face": "special_6"},
        )
        entity.hass = MagicMock()
        entity.async_get_last_state = AsyncMock(return_value=restored)

        with (
            patch.object(sensor, "async_dispatcher_connect", return_value=lambda: None),
            patch.object(entity, "async_write_ha_state"),
        ):
            await entity.async_added_to_hass()

        self.assertEqual(
            coordinator_instance.data["last_session_display_face"], "special_6"
        )
        self.assertEqual(entity.extra_state_attributes["display_face"], "special_6")

    async def test_older_restored_face_cannot_fill_newer_session_null(self) -> None:
        """Restore attributes only when their session timestamp also matches."""
        coordinator_instance, entity = self._entity()
        current_start = coordinator_instance.data["last_session_start"]
        coordinator_instance.data["last_session_display_face"] = None
        restored = types.SimpleNamespace(
            state=(current_start - timedelta(hours=1)).isoformat(),
            attributes={"display_face": "special_6"},
        )
        entity.hass = MagicMock()
        entity.async_get_last_state = AsyncMock(return_value=restored)

        with (
            patch.object(sensor, "async_dispatcher_connect", return_value=lambda: None),
            patch.object(entity, "async_write_ha_state"),
        ):
            await entity.async_added_to_hass()

        self.assertEqual(
            coordinator_instance.data["last_session_start"], current_start
        )
        self.assertIsNone(coordinator_instance.data["last_session_display_face"])
        self.assertIsNone(entity.extra_state_attributes["display_face"])


if __name__ == "__main__":
    unittest.main()
