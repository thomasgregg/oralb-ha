"""Regression tests for the byte-level Oral-B protocol decoders."""

from __future__ import annotations

import importlib.util
import pathlib
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
SPEC.loader.exec_module(protocol)

CONST_PATH = PROTOCOL_PATH.with_name("const.py")
CONST_SPEC = importlib.util.spec_from_file_location("oralb_live_const", CONST_PATH)
assert CONST_SPEC and CONST_SPEC.loader
const = importlib.util.module_from_spec(CONST_SPEC)
CONST_SPEC.loader.exec_module(const)


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

    def test_device_info(self) -> None:
        self.assertEqual(
            protocol.parse_device_info(bytes.fromhex("36 08 52")),
            {
                "model_id": 0x36,
                "protocol_version": 8,
                "firmware_revision": 0x52,
            },
        )

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

    def test_pacer(self) -> None:
        self.assertEqual(
            protocol.parse_pacer(bytes([30, 30, 30, 30, 0, 0, 0, 0])),
            {
                "number_of_sectors": 4,
                "sector_times": [30, 30, 30, 30],
                "target_duration": 120,
            },
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

    def test_sector_sentinels(self) -> None:
        self.assertEqual(
            protocol.decode_sector(0xF0, None, 4),
            ("no_sector", None, 4),
        )
        self.assertEqual(
            protocol.decode_sector(0x07, None, 6),
            ("sector_6", 6, 6),
        )


if __name__ == "__main__":
    unittest.main()
