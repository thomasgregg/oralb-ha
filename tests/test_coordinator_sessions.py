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

try:
    const = importlib.import_module("oralb_live.const")
    coordinator = importlib.import_module("oralb_live.coordinator")
    sensor = importlib.import_module("oralb_live.sensor")
except ImportError as err:  # pragma: no cover - environment without HA
    raise unittest.SkipTest(f"Home Assistant is not importable: {err}") from err


def _advertisement(payload: bytes):
    """A minimal stand-in for what the bluetooth callback hands over."""
    return types.SimpleNamespace(
        manufacturer_data={const.ORALB_MANUFACTURER_ID: payload},
        rssi=-60,
        address="AA:BB:CC:DD:EE:FF",
    )


def _payload(state: int, seconds: int, sector: int = 1, face: int = 0) -> bytes:
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
            sector | (face << 3),
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

    def test_ending_advertisement_keeps_face_with_session(self) -> None:
        """The result face belongs to the session that just ended."""
        c = self._coordinator()
        c._parse_advertisement(_advertisement(_payload(3, 60)))
        c._parse_advertisement(_advertisement(_payload(2, 69, face=5)))

        self.assertEqual(c.data["smiley"], "special_5")
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


if __name__ == "__main__":
    unittest.main()
