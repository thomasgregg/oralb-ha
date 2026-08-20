"""Offline tests for the standalone iO Sense night-light utility."""

from __future__ import annotations

import importlib.util
from pathlib import Path
import unittest


TOOL_PATH = Path(__file__).parents[1] / "tools" / "iosense_night_light.py"
SPEC = importlib.util.spec_from_file_location("iosense_night_light", TOOL_PATH)
assert SPEC is not None and SPEC.loader is not None
tool = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(tool)


class IOSenseNightLightToolTests(unittest.TestCase):
    def test_parse_and_format_color(self):
        self.assertEqual(bytes.fromhex("12abef"), tool.parse_color("#12AbEf"))
        self.assertEqual("#12ABEF", tool.color_text(bytes.fromhex("12abef")))

    def test_parse_color_rejects_bad_values(self):
        for value in ("12345", "#1234567", "red", "#GG0000"):
            with self.subTest(value=value), self.assertRaises(Exception):
                tool.parse_color(value)

    def test_ring_color_get_frames(self):
        self.assertEqual(
            (bytes.fromhex("c036"), bytes.fromhex("e0")),
            tool.get_frames(tool.RING_COLOR),
        )

    def test_ring_color_post_frames(self):
        self.assertEqual(
            (
                bytes.fromhex("c136"),
                bytes.fromhex("ff0080"),
                bytes.fromhex("e0"),
            ),
            tool.post_frames(tool.RING_COLOR, bytes.fromhex("ff0080")),
        )

    def test_mode_post_frames(self):
        self.assertEqual(
            (
                bytes.fromhex("c142"),
                bytes.fromhex("01"),
                bytes.fromhex("e0"),
            ),
            tool.post_frames(
                tool.NIGHT_LIGHT_MODE,
                bytes((tool.NightLightMode.solid,)),
            ),
        )

    def test_mode_values_match_reconstructed_sdk(self):
        self.assertEqual(
            {
                "disabled": 0,
                "solid": 1,
                "breathing": 2,
                "rainbow": 3,
                "cool": 4,
                "custom": 5,
            },
            {mode.name: mode.value for mode in tool.NightLightMode},
        )

    def test_mode_parsers(self):
        self.assertEqual(tool.NightLightMode.solid, tool.parse_mode("SOLID"))
        self.assertEqual(tool.NightLightMode.custom, tool.parse_active_mode("custom"))
        with self.assertRaises(Exception):
            tool.parse_active_mode("disabled")


class FakeChargerClient:
    """In-memory protocol-v2 charger used without bleak or real hardware."""

    def __init__(self) -> None:
        self.color = bytes.fromhex("102030")
        self.mode = tool.NightLightMode.disabled
        self.callbacks = {}
        self.writes = []
        self.header = b""
        self.payload = b""

    async def start_notify(self, uuid, callback):
        self.callbacks[uuid] = callback

    async def stop_notify(self, uuid):
        self.callbacks.pop(uuid)

    async def write_gatt_char(self, uuid, value, response):
        value = bytes(value)
        self.writes.append((uuid, value, response))
        if uuid == tool.WRITE_UUID:
            self.payload = value
            return
        if value != tool.PROTOCOL_END:
            self.header = value
            return

        operation, command = self.header
        if operation == tool.GET:
            payload = (
                self.color
                if command == tool.RING_COLOR
                else bytes((self.mode,))
            )
            self.callbacks[tool.READ_UUID](
                None, bytearray((command, operation)) + bytearray(payload)
            )
        elif operation == tool.POST:
            if command == tool.RING_COLOR:
                self.color = self.payload
            elif command == tool.NIGHT_LIGHT_MODE:
                self.mode = tool.NightLightMode(self.payload[0])
            self.callbacks[tool.STATUS_UUID](
                None, bytearray((command, operation, 1))
            )


class IOSenseNightLightTransactionTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.client = FakeChargerClient()
        self.session = tool.ChargerSession(
            self.client,
            timeout=0.1,
            frame_delay=0,
            settle_delay=0,
        )
        await self.session.start()

    async def asyncTearDown(self):
        await self.session.stop()

    async def test_set_verify_and_restore_roundtrip(self):
        original = await self.session.state()
        self.assertEqual(
            (bytes.fromhex("102030"), tool.NightLightMode.disabled), original
        )

        await self.session.set_color(bytes.fromhex("7a20ff"))
        await self.session.set_mode(tool.NightLightMode.solid)
        self.assertEqual(
            (bytes.fromhex("7a20ff"), tool.NightLightMode.solid),
            await self.session.state(),
        )

        await tool.restore_state(self.session, *original)
        self.assertEqual(original, await self.session.state())

        wire_writes = [value for _uuid, value, _response in self.client.writes]
        self.assertIn(bytes.fromhex("c136"), wire_writes)
        self.assertIn(bytes.fromhex("c142"), wire_writes)
        self.assertTrue(all(response for _uuid, _value, response in self.client.writes))


if __name__ == "__main__":
    unittest.main()
