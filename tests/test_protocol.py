"""Regression tests for the byte-level Oral-B protocol decoders."""

from __future__ import annotations

import importlib.util
import pathlib
import sys
import unittest

PROTOCOL_PATH = (
    pathlib.Path(__file__).parents[1]
    / "custom_components"
    / "oralb_live"
    / "protocol.py"
)
SPEC = importlib.util.spec_from_file_location("oralb_live_protocol", PROTOCOL_PATH)
assert SPEC and SPEC.loader
protocol = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = protocol
SPEC.loader.exec_module(protocol)

CONST_PATH = PROTOCOL_PATH.with_name("const.py")
CONST_SPEC = importlib.util.spec_from_file_location("oralb_live_const", CONST_PATH)
assert CONST_SPEC and CONST_SPEC.loader
const = importlib.util.module_from_spec(CONST_SPEC)
CONST_SPEC.loader.exec_module(const)

CHARGER_PROTOCOL_PATH = PROTOCOL_PATH.with_name("charger_protocol.py")
CHARGER_SPEC = importlib.util.spec_from_file_location(
    "oralb_live_charger_protocol", CHARGER_PROTOCOL_PATH
)
assert CHARGER_SPEC and CHARGER_SPEC.loader
charger_protocol = importlib.util.module_from_spec(CHARGER_SPEC)
sys.modules[CHARGER_SPEC.name] = charger_protocol
CHARGER_SPEC.loader.exec_module(charger_protocol)


class ProtocolDecoderTests(unittest.TestCase):
    """Exercise known protocol 8 and compatibility payloads."""

    def test_battery_status_protocol_8(self) -> None:
        payload = bytes(
            [
                95,
                0x10,
                0x0E,  # 3600 seconds
                0xA0,
                0x0F,  # 4000 mV
                0x7B,
                0x00,  # 123 mA
                25,
            ]
        )
        self.assertEqual(
            protocol.parse_battery_status(payload),
            {
                "battery": 95,
                "battery_time_remaining": 3600,
                "battery_voltage": 4.0,
                "battery_current": 123,
                "battery_temperature": 25,
            },
        )

    def test_battery_status_old_brush(self) -> None:
        self.assertEqual(protocol.parse_battery_status(bytes([80])), {"battery": 80})

    def test_battery_status_signed_current(self) -> None:
        payload = bytes([97, 0, 0, 0, 0, 0x42, 0xFD, 35])
        self.assertEqual(
            -702, protocol.parse_battery_status(payload)["battery_current"]
        )

    def test_device_info(self) -> None:
        self.assertEqual(
            protocol.parse_device_info(bytes.fromhex("36 08 52")),
            {
                "model_id": 0x36,
                "protocol_version": 8,
                "firmware_revision": 0x52,
            },
        )

    def test_brush_device_name(self) -> None:
        self.assertEqual(
            "iO Series Toothbrush 64D3",
            const.brush_device_name("58:26:3A:F6:64:D3", "iO Series"),
        )
        self.assertEqual(
            "Oral-B Toothbrush 64D3",
            const.brush_device_name("58:26:3A:F6:64:D3"),
        )

    def test_brush_firmware_version_matches_app_format(self) -> None:
        self.assertEqual("00.82.26", const.brush_firmware_version(82, 0, 26))
        self.assertEqual("82", const.brush_firmware_version(82))
        self.assertEqual("00.00.00", const.brush_firmware_version(0, 0, 0))
        self.assertIsNone(const.brush_firmware_version(None, 0, 26))

    def test_io_mode_mapping(self) -> None:
        self.assertEqual(const.MODES[5], "super_sensitive")
        self.assertEqual(const.MODES[6], "tongue_clean")
        self.assertEqual(const.MODES[9], "off")
        self.assertEqual(const.MODES[11], "smart_adapt")

    def test_terminal_state_mapping(self) -> None:
        self.assertEqual(const.STATES[113], "final_test")
        self.assertEqual(const.STATES[114], "pcb_test")
        self.assertEqual(const.STATES[115], "sleeping")
        self.assertEqual(const.STATES[116], "transport")
        self.assertEqual(const.RELEASE_STATES, {115, 116})
        self.assertEqual(10, const.MIN_PASSIVE_SESSION_SECONDS)

    def test_charger_refreshes_current_values_before_retained_session(self) -> None:
        self.assertEqual(
            ("FF05", "FF2B", "FF2D"), const.CHARGER_POST_SESSION_READS[:3]
        )
        self.assertLess(
            const.CHARGER_POST_SESSION_READS.index("FF2B"),
            const.CHARGER_POST_SESSION_READS.index("FF29"),
        )
        self.assertLess(
            const.CHARGER_POST_SESSION_READS.index("FF2D"),
            const.CHARGER_POST_SESSION_READS.index("FF29"),
        )
        self.assertLess(
            const.CHARGER_POST_SESSION_READS.index("FF05"),
            const.CHARGER_POST_SESSION_READS.index("FF29"),
        )
        self.assertLess(
            const.CHARGER_POST_SESSION_READS.index("FF02"),
            const.CHARGER_POST_SESSION_READS.index("FF29"),
        )
        self.assertLess(
            const.CHARGER_POST_SESSION_READS.index("FF0A"),
            const.CHARGER_POST_SESSION_READS.index("FF29"),
        )
        self.assertEqual(30, const.CHARGER_BATTERY_EVERY_TICKS)
        self.assertEqual(5, const.CHARGER_BRUSH_STATUS_EVERY_TICKS)

    def test_pacer(self) -> None:
        self.assertEqual(
            protocol.parse_pacer(bytes([30, 30, 30, 30, 0, 0, 0, 0])),
            {
                "number_of_sectors": 4,
                "sector_times": [30, 30, 30, 30],
                "target_duration": 120,
            },
        )

    def test_pacer_progress_from_elapsed_time(self) -> None:
        sector_times = [30, 30, 30, 30]
        self.assertEqual(protocol.derive_pacer_progress(0, sector_times), (1, 0))
        self.assertEqual(protocol.derive_pacer_progress(29, sector_times), (1, 29))
        self.assertEqual(protocol.derive_pacer_progress(30, sector_times), (2, 0))
        self.assertEqual(protocol.derive_pacer_progress(91, sector_times), (4, 1))
        self.assertEqual(protocol.derive_pacer_progress(135, sector_times), (4, 45))

    def test_pacer_progress_advances_from_ff09_anchor(self) -> None:
        sector_times = [30, 30, 30, 30]
        self.assertEqual(
            protocol.advance_pacer_progress(1, 28, 1, sector_times),
            (1, 29),
        )
        self.assertEqual(
            protocol.advance_pacer_progress(1, 28, 2, sector_times),
            (2, 0),
        )
        self.assertEqual(
            protocol.advance_pacer_progress(4, 29, 10, sector_times),
            (4, 39),
        )

    def test_refill_remainder(self) -> None:
        self.assertEqual(
            protocol.parse_refill_remainder(bytes.fromhex("00 1e 00 58 02")),
            {
                "refill_state_raw": 0,
                "refill_days": 30,
                "refill_brushing_time": 600,
            },
        )

    def test_ring_color(self) -> None:
        self.assertEqual(
            {
                "ring_color": "#44CF63",
                "ring_color_fourth_byte": 0,
            },
            protocol.parse_ring_color(bytes.fromhex("44 cf 63 00")),
        )
        self.assertEqual(
            "#44CF63",
            protocol.parse_ring_color(bytes.fromhex("44 cf 63 00 aa"))["ring_color"],
        )
        self.assertEqual({}, protocol.parse_ring_color(bytes.fromhex("44 cf 63")))

    def test_session_timer_requires_forward_progress(self) -> None:
        baseline, confirmed = protocol.advance_session_timer_evidence(None, 77)
        self.assertEqual(77, baseline)
        self.assertFalse(confirmed)

        baseline, confirmed = protocol.advance_session_timer_evidence(baseline, 77)
        self.assertEqual(77, baseline)
        self.assertFalse(confirmed)

        baseline, confirmed = protocol.advance_session_timer_evidence(baseline, 0)
        self.assertEqual(0, baseline)
        self.assertFalse(confirmed)

        baseline, confirmed = protocol.advance_session_timer_evidence(baseline, 2)
        self.assertEqual(0, baseline)
        self.assertTrue(confirmed)

    def test_available_modes_are_unique(self) -> None:
        self.assertEqual(
            protocol.parse_available_modes(bytes([0, 1, 4, 5, 6, 11, 0xFF, 0xFF])),
            [0, 1, 4, 5, 6, 11],
        )

    def test_sector_masks_display_bits(self) -> None:
        self.assertEqual(
            protocol.decode_sector(0b10100011, 4, None),
            ("sector_3", 3, 4),
        )

    def test_display_face_from_sector_byte(self) -> None:
        expected = ["off", "standard"] + [
            f"special_{face}" for face in range(2, 8)
        ]
        for face, name in enumerate(expected):
            # Sector and unrelated high bits must not leak into the face.
            raw = 0xC0 | (face << 3) | 0x05
            decoded = protocol.decode_display_face(raw)
            self.assertEqual(face, decoded)
            self.assertEqual(name, const.SMILEYS[decoded])

        # Captured session-end byte: face 6 plus last-sector sentinel 7.
        self.assertEqual(6, protocol.decode_display_face(0x37))

    def test_sector_sentinels(self) -> None:
        self.assertEqual(
            protocol.decode_sector(0xF0, None, 4),
            ("no_sector", None, 4),
        )
        self.assertEqual(
            protocol.decode_sector(0x07, None, 6),
            ("sector_6", 6, 6),
        )

    def test_charger_sector_is_zero_based(self) -> None:
        self.assertEqual(
            protocol.decode_charger_sector(0, 6, None),
            ("sector_1", 1, 6),
        )
        self.assertEqual(
            protocol.decode_charger_sector(3, None, 6),
            ("sector_4", 4, 6),
        )
        self.assertEqual(
            protocol.decode_charger_sector(0xF0, None, 6),
            ("no_sector", None, 6),
        )

    def test_verified_protocol_8_session_record(self) -> None:
        parsed = protocol.parse_session_record(
            bytes.fromhex("26e4ff3161017800800064000a001321280201045e")
        )
        self.assertEqual(838853670, parsed["session_timestamp"])
        self.assertEqual(353, parsed["session_id"])
        self.assertEqual(120, parsed["target_duration"])
        self.assertEqual(128, parsed["duration"])
        self.assertEqual(10.0, parsed["high_pressure_time"])
        self.assertEqual(1.0, parsed["low_pressure_time"])
        self.assertEqual(1900, parsed["average_pressure"])
        self.assertEqual(3300, parsed["maximum_pressure"])
        self.assertEqual(40, parsed["high_pressure_events"])
        self.assertEqual(2, parsed["low_pressure_events"])
        self.assertEqual(4, parsed["mode_raw"])
        self.assertEqual(94, parsed["battery_end"])

    def test_session_record_ignores_unknown_battery(self) -> None:
        parsed = protocol.parse_session_record(
            bytes.fromhex("26e4ff3161017800800064000a00132128020104ff")
        )
        self.assertNotIn("battery_end", parsed)

    def test_comino_sensor_snapshot(self) -> None:
        records = protocol.parse_comino_sensor_snapshot(
            bytes.fromhex("f49bfcf8011612f0cc9bfbf8021612f000001080")
        )
        self.assertEqual(len(records), 2)
        self.assertEqual(records[0].timestamp, 39924)
        self.assertEqual(
            (
                records[0].gyro_x,
                records[0].gyro_y,
                records[0].gyro_z,
                records[0].motion_x,
                records[0].motion_y,
                records[0].motion_z,
            ),
            (-4, -8, 1, 22, 18, -16),
        )
        self.assertEqual(records[1].timestamp, 39884)

    def test_comino_sensor_snapshot_rejects_other_formats(self) -> None:
        with self.assertRaisesRegex(ValueError, "20 bytes"):
            protocol.parse_comino_sensor_snapshot(b"\x00" * 19)
        with self.assertRaisesRegex(ValueError, "not Comino"):
            protocol.parse_comino_sensor_snapshot(b"\x00" * 20)
        with self.assertRaisesRegex(ValueError, "not Comino"):
            protocol.parse_comino_sensor_snapshot(b"\x00" * 18 + b"\x10\x00")


class ChargerProtocolTests(unittest.TestCase):
    """Exercise captured iO Sense packets and read-only frame builders."""

    def test_charger_session_prefers_active_brush_status(self) -> None:
        self.assertTrue(
            charger_protocol.resolve_charger_session_running("inactive", "pre_run")
        )
        self.assertTrue(
            charger_protocol.resolve_charger_session_running("inactive", "run")
        )

    def test_charger_session_prefers_quiet_brush_status(self) -> None:
        for brush_status in ("not_connected", "idle", "charging"):
            with self.subTest(brush_status=brush_status):
                self.assertFalse(
                    charger_protocol.resolve_charger_session_running(
                        "active_running", brush_status, True
                    )
                )

    def test_charger_session_falls_back_to_native_status(self) -> None:
        self.assertTrue(
            charger_protocol.resolve_charger_session_running("active_running", None)
        )
        self.assertFalse(
            charger_protocol.resolve_charger_session_running("inactive", None, True)
        )
        self.assertTrue(
            charger_protocol.resolve_charger_session_running(None, "unknown", True)
        )
        self.assertTrue(
            charger_protocol.resolve_charger_session_running(
                "inactive", "unknown", True
            )
        )
        self.assertTrue(
            charger_protocol.resolve_charger_session_running(
                "inactive", "unknown_0xfe", True
            )
        )
        self.assertTrue(
            charger_protocol.resolve_charger_session_running("inactive", "", True)
        )

    def test_charger_session_observed_firmware_sequence(self) -> None:
        running = False
        running = charger_protocol.resolve_charger_session_running(
            "inactive", None, running
        )
        self.assertFalse(running)
        running = charger_protocol.resolve_charger_session_running(
            "inactive", "pre_run", running
        )
        self.assertTrue(running)
        running = charger_protocol.resolve_charger_session_running(
            "inactive", "unknown", running
        )
        self.assertTrue(running)
        running = charger_protocol.resolve_charger_session_running(
            "inactive", "charging", running
        )
        self.assertFalse(running)

    def test_charger_live_auxiliary_schedule(self) -> None:
        def auxiliary(tick: int, *, mode_observed: bool = True) -> str:
            return charger_protocol.charger_live_auxiliary(
                tick,
                mode_observed=mode_observed,
                brush_status_every_ticks=const.CHARGER_BRUSH_STATUS_EVERY_TICKS,
                battery_every_ticks=const.CHARGER_BATTERY_EVERY_TICKS,
            )

        expected_startup = ["FF05", "FF07", "FF26", "FF2D", "FF08"]
        self.assertEqual(
            expected_startup,
            [auxiliary(tick) for tick in range(5)],
        )
        self.assertEqual("BRUSH_STATUS", auxiliary(5))
        self.assertEqual("FF05", auxiliary(29))
        self.assertEqual("BRUSH_STATUS", auxiliary(30))
        self.assertEqual("FF07", auxiliary(6, mode_observed=False))
        self.assertEqual("FF08", auxiliary(6))

    def test_charger_advertisement(self) -> None:
        parsed = charger_protocol.decode_charger_advertisement(
            bytes.fromhex("02a2000304015c013bdbb8a20157")
        )
        self.assertEqual("0.3.4", parsed["firmware"])
        self.assertEqual("5C:01:3B:DB:B8:A2", parsed["mac"])
        self.assertTrue(parsed["wifi_connected"])
        self.assertTrue(parsed["internet_connected"])
        self.assertTrue(parsed["cloud_connected"])
        self.assertFalse(parsed["brush_connected"])
        self.assertTrue(parsed["brush_charging"])
        self.assertTrue(parsed["brush_paired"])

    def test_charger_native_packets(self) -> None:
        self.assertEqual(
            "connected_with_internet",
            charger_protocol.decode_charger_read(bytes.fromhex("14c004")).value,
        )
        self.assertEqual(
            "connected",
            charger_protocol.decode_charger_read(bytes.fromhex("16c003")).value,
        )
        self.assertEqual(
            -65,
            charger_protocol.decode_charger_read(bytes.fromhex("15c03e")).value,
        )

    def test_charger_brush_identity(self) -> None:
        raw = bytes.fromhex(
            "39c00158263af664d35c0100000000ff9b10361100014fb7afea2b3d4298954287b8bc4cdf"
            "35e40c07085206560300561a0100000000000056527200000000000000"
        )
        value = charger_protocol.decode_charger_read(raw).value
        self.assertEqual("58:26:3A:F6:64:D3", value["brush_mac"])
        self.assertEqual(54, value["model_id"])
        self.assertEqual(8, value["protocol_version"])
        self.assertEqual(82, value["firmware_revision"])
        self.assertEqual(6, value["hardware_version"])
        self.assertEqual(3, value["bootloader_version"])
        self.assertEqual(26, value["media_content_version"])
        self.assertEqual(1, value["hardware_configuration"])
        self.assertEqual(0, value["memory_map_version"])
        self.assertEqual(0, value["info_sector_version"])
        self.assertEqual(0, value["second_controller_version"])

    def test_charger_brush_identity_ignores_truncated_controller_metadata(
        self,
    ) -> None:
        raw = bytes.fromhex(
            "39c00158263af664d35c0100000000ff9b10361100014fb7afea2b3d4298954287b8bc4cdf"
            "35e40c07085206560300561a0100000000"
        )
        value = charger_protocol.decode_charger_read(raw).value
        self.assertEqual(82, value["firmware_revision"])
        self.assertNotIn("second_controller_version", value)

    def test_charger_passthrough(self) -> None:
        packet = charger_protocol.decode_charger_read(
            bytes.fromhex("37c108ff010102012a")
        )
        self.assertEqual("FF08", packet.value[0]["short_uuid"])
        self.assertTrue(packet.value[0]["success"])
        self.assertEqual(bytes.fromhex("012a"), packet.value[0]["data"])
        self.assertEqual(
            bytes.fromhex("08ff0100"),
            charger_protocol.build_passthrough_read("ff08"),
        )

        color_packet = charger_protocol.decode_charger_read(
            bytes.fromhex("37c12bff01010444cf6300")
        )
        self.assertEqual("FF2B", color_packet.value[0]["short_uuid"])
        self.assertTrue(color_packet.value[0]["success"])
        self.assertEqual(bytes.fromhex("44cf6300"), color_packet.value[0]["data"])

    def test_normalize_mac(self) -> None:
        self.assertEqual(
            "58263AF664D3", charger_protocol.normalize_mac("58:26:3a:f6:64:d3")
        )
        self.assertIsNone(charger_protocol.normalize_mac("not-a-mac"))


if __name__ == "__main__":
    unittest.main()
