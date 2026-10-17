"""Hardware-feedback regressions using production delays and the real tracker."""

from __future__ import annotations

import asyncio
import unittest
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from unittest.mock import patch
from zoneinfo import ZoneInfo

import habluetooth.manager as bluetooth_manager

from tests import test_battery_refresh as fixtures

WALL_START = fixtures.WALL_START
const = fixtures.const
coordinator = fixtures.coordinator


@asynccontextmanager
async def brush():
    """Reuse transport/clock fixtures without collecting the original tests twice."""
    h = fixtures.BatteryRefreshTests()
    await h.asyncSetUp()
    try:
        yield h
    finally:
        if h.manager._cancel_unavailable_tracking:
            h.manager._cancel_unavailable_tracking.cancel()
        await h.asyncTearDown()


class ProvisionalSessionRecoveryTests(unittest.IsolatedAsyncioTestCase):
    async def test_overnight_menu_recovery_matrix(self):
        for previous in (False, True):
            for retained in (0, 90, 120):
                for running in (False, True):
                    with self.subTest(previous=previous, timer=retained, running=running):
                        async with brush() as h:
                            if previous:
                                await h._stop_session(face=3)
                                await h._advance(31)
                                await h._feed(2, 120)
                            else:
                                h.c.async_start()
                            await h._feed(8, retained)
                            abandoned = h.c._session_start
                            h.c._async_unavailable(None)
                            h.clock.now += 86400
                            await h._advance(0)
                            h.c._async_reset_sessions_today(coordinator.dt_util.now())
                            await h._feed(3 if running else 8, 0)
                            await h._advance(1)
                            await h._feed(3 if running else 8, 1)
                            await h._advance(119)
                            await h._feed(9, 120)
                            await h._advance(20)
                            self.assertEqual(h.c.data["sessions_today"], 1)
                            self.assertNotEqual(h.c.data["last_session_start"], abandoned)
                            self.assertEqual(h.c.data["last_session_duration"], 120)

    async def test_actual_home_assistant_unavailable_delivery(self):
        async with brush() as h:
            h.manager._loop = h.hass.loop
            h.c.async_start()
            await h._feed(8, 120)
            h.clock.now += 86400
            with patch.object(bluetooth_manager, "monotonic_time_coarse", return_value=h.clock.now):
                h.manager._async_check_unavailable()
            self.assertFalse(h.c._advertisement_available)
            self.assertFalse(h.manager._all_history)
            await h._feed(3, 0)
            await h._advance(120)
            await h._feed(9, 120)
            await h._advance(20)
            self.assertEqual(h.c.data["sessions_today"], 1)

    async def test_repeated_loss_preserves_completed_summary(self):
        async with brush() as h:
            await h._stop_session(face=3)
            await h._advance(31)
            await h._feed(2, 120)
            saved = {k: v for k, v in h.c.data.items() if k.startswith("last_session_") or k == "sessions_today"}
            await h._feed(8, 120)
            generation = h.c._session_generation
            h.c._async_unavailable(None)
            h.c._async_unavailable(None)
            self.assertFalse(h.c._session_active)
            self.assertEqual(h.c._session_generation, generation)
            self.assertEqual(saved, {k: v for k, v in h.c.data.items() if k in saved})

    async def test_loss_during_resume_candidate_preserves_one_session(self):
        async with brush() as h:
            await h._stop_session(face=0)
            start = h.c._session_start
            await h._advance(5)
            await h._feed(8, 120)
            h.c._async_unavailable(None)
            self.assertIsNone(h.c.data["last_session_start"])
            self.assertEqual(h.c._session_generation, 1)
            await h._advance(2)
            await h._feed(3, 120)
            await h._advance(1)
            await h._feed(3, 121)
            await h._advance(2)
            await h._feed(9, 123, face=4)
            await h._advance(20)
            self.assertEqual(h.c.data["sessions_today"], 1)
            self.assertEqual(h.c.data["last_session_start"], start)
            self.assertEqual(h.c.data["last_session_duration"], 123)
            self.assertEqual(h.c.data["last_session_display_face"], "special_4")

    async def test_loss_during_resume_preserves_original_finalize_deadline(self):
        async with brush() as h:
            await h._stop_session(face=0)
            await h._advance(5)
            await h._feed(8, 120)
            h.c._async_unavailable(None)
            h.c._apply_smiley(b"\x03", source=const.DATA_SOURCE_DIRECT)
            await h._advance(14)
            self.assertIsNone(h.c.data["last_session_start"])
            await h._advance(1)
            self.assertEqual(h.c.data["sessions_today"], 1)
            self.assertEqual(h.c.data["last_session_display_face"], "special_3")

    async def test_reset_after_lost_resume_is_a_second_session(self):
        async with brush() as h:
            await h._stop_session(face=3)
            await h._advance(5)
            await h._feed(8, 120)
            h.c._async_unavailable(None)
            await h._feed(3, 0)
            await h._advance(1)
            await h._feed(3, 1)
            await h._advance(4)
            await h._feed(9, 5, face=4)
            await h._advance(20)
            self.assertEqual(h.c.data["sessions_today"], 2)
            self.assertEqual(h.c.data["last_session_duration"], 5)

    async def test_confirmed_sessions_survive_brush_advertisement_loss(self):
        for kind in ("passive", "menu", "direct", "charger"):
            with self.subTest(kind=kind):
                async with brush() as h:
                    h.c.async_start()
                    if kind == "charger":
                        h.c.charger._session_running = True
                        h.c.charger._session_confirmed = True
                        h.c._charger_session_started(confirmed=True)
                    else:
                        if kind == "direct":
                            h.c.mode = const.CONNECTION_MODE_LIVE
                            h.c._schedule_connect = lambda: None
                        await h._feed(8 if kind == "menu" else 3, 0)
                        if kind == "menu":
                            await h._advance(1)
                            await h._feed(8, 1)
                    start = h.c._session_start
                    generation = h.c._session_generation
                    h.c._async_unavailable(None)
                    h.c._async_unavailable(None)
                    self.assertTrue(h.c._session_active)
                    self.assertTrue(h.c._session_confirmed)
                    self.assertEqual(h.c._session_start, start)
                    self.assertEqual(h.c._session_generation, generation)

    async def test_motor_start_after_midnight_does_not_inherit_menu_date(self):
        for zone in ("UTC", "Europe/Berlin", "America/New_York"):
            with self.subTest(zone=zone):
                async with brush() as h:
                    tz = ZoneInfo(zone)
                    with patch.object(coordinator.dt_util.original, "DEFAULT_TIME_ZONE", tz):
                        before = datetime(2026, 10, 1, 23, 59, tzinfo=tz)
                        h.clock.now = 1000 + (before.astimezone(timezone.utc) - WALL_START).total_seconds()
                        h.c.async_start()
                        await h._feed(8, 0)
                        await h._advance(120)
                        h.c._async_reset_sessions_today(coordinator.dt_util.now())
                        start = h.clock.utcnow()
                        await h._feed(3, 0)
                        await h._advance(120)
                        await h._feed(9, 120)
                        await h._advance(20)
                        self.assertEqual(h.c.data["last_session_start"], start)
                        self.assertEqual(h.c.data["sessions_today"], 1)

    async def test_confirmed_brushing_across_midnight_keeps_original_date(self):
        async with brush() as h:
            h.clock.now = 1000 + (datetime(2026, 10, 1, 23, 59, tzinfo=timezone.utc) - WALL_START).total_seconds()
            h.c.async_start()
            await h._feed(3, 0)
            start = h.c._session_start
            await h._advance(120)
            h.c._async_reset_sessions_today(coordinator.dt_util.now())
            await h._feed(9, 120)
            await h._advance(20)
            self.assertEqual(h.c.data["last_session_start"], start)
            self.assertEqual(h.c.data["sessions_today"], 0)

    async def test_timer_confirmation_dates_brushing_not_abandoned_menu(self):
        async with brush() as h:
            h.c.async_start()
            await h._feed(8, 0)
            await h._advance(600)
            await h._feed(8, 0)
            start = h.clock.utcnow()
            await h._advance(1)
            await h._feed(8, 1)
            await h._advance(119)
            await h._feed(9, 120)
            await h._advance(20)
            self.assertEqual(h.c.data["last_session_start"], start)

    async def test_cached_continuation_candidate_expires_without_unavailable(self):
        for running in (True, False):
            with self.subTest(running=running):
                async with brush() as h:
                    await h._stop_session(face=3)
                    await h._advance(21)
                    await h._feed(8, 120)
                    self.assertIsNotNone(h.c._late_continuation_candidate)
                    await h._advance(121)
                    await h._feed(3 if running else 8, 121)
                    await h._advance(1)
                    await h._feed(3 if running else 8, 122)
                    await h._feed(9, 122)
                    await h._advance(20)
                    self.assertEqual(h.c.data["sessions_today"], 2)


class MaintenanceFaceCaptureTests(unittest.IsolatedAsyncioTestCase):
    async def test_first_connection_captures_ff0a_without_advertised_face(self):
        for protocol in (6, 7, 8):
            for raw, face in ((1, "standard"), (3, "special_3"), (255, "face_255")):
                with self.subTest(protocol=protocol, raw=raw):
                    async with brush() as h:
                        h.responses[const.CHAR_SMILEY] = bytes((raw,))
                        await h._stop_session(face=0, protocol=protocol)
                        h._assert_published_battery(23)
                        self.assertEqual([uuid for _, uuid in h.reads], [const.CHAR_STATUS_BLOB, const.CHAR_SMILEY])
                        await h._advance(20)
                        self.assertEqual(h.c.data["last_session_display_face"], face)
                        self.assertEqual(h.c.data["last_session_display_face_source"], const.DATA_SOURCE_DIRECT)
                        self.assertEqual(h.c.data["sessions_today"], 1)
                        self.assertEqual(len(h.connections), 1)

    async def test_delayed_result_retries_with_production_timing(self):
        async with brush() as h:
            def response(uuid):
                if uuid == const.CHAR_STATUS_BLOB:
                    return b"\x17"
                if uuid == const.CHAR_SMILEY:
                    return b"\x03" if h.clock.now >= 1122 else b"\x00"
                return None
            async def read(uuid):
                return response(uuid)
            h.read_hook = read
            await h._stop_session(face=0)
            h._assert_published_battery(23)
            await h._advance(20)
            times = [at - 1120 for at, uuid in h.reads if uuid == const.CHAR_SMILEY]
            self.assertEqual(times, [0, 0.5, 1.5, 3.5])
            self.assertEqual(h.c.data["last_session_display_face"], "special_3")
            self.assertEqual(len(h.connections), 1)
            self.assertNotIn(const.CHAR_SESSION_DATA, [uuid for _, uuid in h.reads])

    async def test_missing_off_or_failed_faces_preserve_battery_and_null(self):
        for response in (None, b"", b"\x00", coordinator.BleakError("unsupported")):
            with self.subTest(response=response):
                async with brush() as h:
                    h.responses[const.CHAR_SMILEY] = response
                    await h._stop_session(face=0)
                    await h._advance(20)
                    self.assertEqual(h.c.data["battery"], 23)
                    self.assertIsNone(h.c.data["last_session_display_face"])
                    self.assertFalse(h.client.is_connected)
                    self.assertEqual(len(h.connections), 1)

    async def test_face_success_does_not_resolve_failed_battery(self):
        async with brush() as h:
            h.status = None
            h.responses[const.CHAR_SMILEY] = b"\x01"
            await h._stop_session(face=0)
            await h._advance(20)
            self.assertEqual(h.c.data["battery"], 99)
            self.assertTrue(h.c._maintenance_pending)
            self.assertEqual(h.c.data["last_session_display_face"], "standard")

    async def test_face_success_does_not_resolve_pending_record(self):
        async with brush() as h:
            h.responses[const.CHAR_SMILEY] = b"\x03"
            await h._stop_session(face=0)
            self.assertTrue(h.c._session_pending_sync)
            self.assertLess(h.c._processed_session_generation, h.c._session_generation)
            self.assertNotIn(const.CHAR_SESSION_DATA, [uuid for _, uuid in h.reads])

    async def test_advertisement_face_avoids_redundant_capture_read(self):
        async with brush() as h:
            h.responses[const.CHAR_SMILEY] = b"\x07"
            await h._stop_session(face=3)
            await h._advance(20)
            self.assertEqual(h.c.data["last_session_display_face"], "special_3")
            self.assertNotIn(const.CHAR_SMILEY, [uuid for _, uuid in h.reads])

    async def test_sleep_running_or_charger_discovery_ends_face_retries(self):
        for state in (115, 3, "charger"):
            with self.subTest(state=state):
                async with brush() as h:
                    h.responses[const.CHAR_SMILEY] = b"\x00"
                    await h._stop_session(face=0)
                    if state == "charger":
                        h.c.charger.address = "11:22:33:44:55:66"
                    else:
                        await h._feed(state, 120)
                    await h._advance(4)
                    self.assertEqual([u for _, u in h.reads].count(const.CHAR_SMILEY), 1)
                    self.assertFalse(h.client.is_connected)

    async def test_new_advertised_face_wins_over_outstanding_read(self):
        async with brush() as h:
            release = asyncio.Event()
            async def read(uuid):
                if uuid == const.CHAR_STATUS_BLOB:
                    return b"\x17"
                if uuid == const.CHAR_SMILEY:
                    await release.wait()
                    return b"\x07"
                return None
            h.read_hook = read
            await h._stop_session(face=0)
            await h._feed(9, 120, face=3)
            self.assertIn(const.CHAR_SMILEY, [uuid for _, uuid in h.reads])
            release.set()
            await h._ready()
            await h._advance(20)
            self.assertEqual(h.c.data["last_session_display_face"], "special_3")
            self.assertEqual(h.c.data["smiley"], "special_3")

    async def test_old_outstanding_face_cannot_attach_to_new_session(self):
        async with brush() as h:
            release = asyncio.Event()
            reached = asyncio.Event()
            async def read(uuid):
                if uuid == const.CHAR_STATUS_BLOB:
                    return b"\x17"
                if uuid == const.CHAR_SMILEY:
                    reached.set()
                    await release.wait()
                    return b"\x03"
                return None
            h.read_hook = read
            await h._stop_session(face=0)
            await h._advance(60)
            reached.clear()
            await h._feed(2, 120)
            self.assertTrue(reached.is_set())
            await h._feed(3, 0)
            await h._advance(1)
            await h._feed(3, 1)
            await h._advance(1)
            await h._feed(9, 2, face=0)
            release.set()
            await h._ready()
            await h._advance(20)
            self.assertIsNone(h.c.data["last_session_display_face"])
            self.assertEqual(h.c.data["sessions_today"], 2)

    async def test_face_applied_before_later_optional_read(self):
        async with brush() as h:
            release = asyncio.Event()
            async def read(uuid):
                if uuid == const.CHAR_STATUS_BLOB:
                    return b"\x17"
                if uuid == const.CHAR_SMILEY:
                    return b"\x03"
                if uuid == const.CHAR_REFILL_REMAINDER:
                    await release.wait()
                return None
            h.read_hook = read
            h.c.async_start()
            await h._feed(2)
            self.assertEqual(h.c.data["smiley"], "special_3")
            await h._feed(3, 0)
            await h._advance(1)
            await h._feed(3, 1)
            await h._advance(1)
            await h._feed(9, 2, face=0)
            release.set()
            await h._ready()
            await h._advance(20)
            self.assertIsNone(h.c.data["last_session_display_face"])

    async def test_stalled_face_read_is_bounded_and_battery_already_published(self):
        async with brush() as h:
            blocked = asyncio.Event()
            async def read(uuid):
                if uuid == const.CHAR_STATUS_BLOB:
                    return b"\x17"
                if uuid == const.CHAR_SMILEY:
                    await blocked.wait()
                return None
            h.read_hook = read
            await h._stop_session(face=0)
            self.assertIn(const.CHAR_SMILEY, [uuid for _, uuid in h.reads])
            h._assert_published_battery(23)
            await h._advance(21)
            self.assertFalse(h.client.is_connected)
            self.assertIsNone(h.c.data["last_session_display_face"])

    async def test_unload_during_face_read_disconnects_and_keeps_battery(self):
        async with brush() as h:
            blocked = asyncio.Event()
            async def read(uuid):
                if uuid == const.CHAR_STATUS_BLOB:
                    return b"\x17"
                if uuid == const.CHAR_SMILEY:
                    await blocked.wait()
                return None
            h.read_hook = read
            await h._stop_session(face=0)
            self.assertIn(const.CHAR_SMILEY, [uuid for _, uuid in h.reads])
            await h.c.async_stop()
            self.assertFalse(h.client.is_connected)
            self.assertEqual(h.c.data["battery"], 23)
            self.assertEqual(h.c.data["sessions_today"], 1)

    async def test_cooldown_does_not_reconnect_only_to_capture_face(self):
        async with brush() as h:
            h.c.async_start()
            await h._feed(3, 0)
            await h._advance(120)
            h.c._last_sync_attempt = h.clock.now - 10
            await h._feed(9, 120)
            await h._advance(20)
            self.assertEqual(h.connections, [])
            self.assertIsNone(h.c.data["last_session_display_face"])

    async def test_result_arriving_at_or_after_capture_deadline(self):
        for latency, expected in ((1.5, "special_3"), (2.0, None)):
            with self.subTest(latency=latency):
                async with brush() as h:
                    async def connect(*args, **kwargs):
                        await h.clock.sleep(13.5)
                        return await h._connect()
                    async def read(uuid):
                        if uuid == const.CHAR_STATUS_BLOB:
                            return b"\x17"
                        if uuid == const.CHAR_SMILEY:
                            await h.clock.sleep(latency)
                            return b"\x03"
                        return None
                    h.c._async_establish_brush_connection.side_effect = connect
                    h.read_hook = read
                    await h._stop_session(face=0)
                    await h._advance(20)
                    self.assertEqual(h.c.data["last_session_display_face"], expected)
                    self.assertEqual(h.c.data["battery"], 23)
                    self.assertFalse(h.client.is_connected)

    async def test_expired_connection_does_not_extend_capture_window(self):
        async with brush() as h:
            async def connect(*args, **kwargs):
                await h.clock.sleep(16)
                return await h._connect()
            h.c._async_establish_brush_connection.side_effect = connect
            h.responses[const.CHAR_SMILEY] = b"\x03"
            await h._stop_session(face=0)
            await h._advance(20)
            self.assertIsNone(h.c.data["last_session_display_face"])
            self.assertNotIn(const.CHAR_SMILEY, [uuid for _, uuid in h.reads])
            self.assertFalse(h.client.is_connected)

    async def test_unload_during_retry_sleep_disconnects_without_another_read(self):
        async with brush() as h:
            h.responses[const.CHAR_SMILEY] = b"\x00"
            await h._stop_session(face=0)
            self.assertTrue(h.client.is_connected)
            await h.c.async_stop()
            await h._advance(4)
            self.assertFalse(h.client.is_connected)
            self.assertEqual([u for _, u in h.reads].count(const.CHAR_SMILEY), 1)

    async def test_outstanding_face_does_not_seed_new_active_session(self):
        async with brush() as h:
            release = asyncio.Event()
            async def read(uuid):
                if uuid == const.CHAR_STATUS_BLOB:
                    return b"\x17"
                if uuid == const.CHAR_SMILEY:
                    await release.wait()
                    return b"\x03"
                return None
            h.read_hook = read
            await h._stop_session(face=0)
            await h._feed(3, 0)
            release.set()
            await h._ready()
            await h._advance(1)
            await h._feed(3, 1)
            await h._feed(9, 1)
            await h._advance(20)
            self.assertIsNone(h.c.data["last_session_display_face"])
