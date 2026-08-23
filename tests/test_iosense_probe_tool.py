"""Offline tests for the standalone iO Sense diagnostic probe."""

from __future__ import annotations

import importlib.util
from pathlib import Path
import sys
import unittest


TOOL_PATH = Path(__file__).parents[1] / "tools" / "iosense_probe.py"
SPEC = importlib.util.spec_from_file_location("iosense_probe", TOOL_PATH)
assert SPEC is not None and SPEC.loader is not None
tool = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = tool
SPEC.loader.exec_module(tool)


class FakeDevice:
    def __init__(self, address: str, name: str | None = None) -> None:
        self.address = address
        self.name = name


class FakeAdvertisement:
    def __init__(
        self,
        *,
        local_name: str | None = None,
        rssi: int = -60,
        service_uuids=(),
        manufacturer_data=None,
        service_data=None,
        tx_power=None,
    ) -> None:
        self.local_name = local_name
        self.rssi = rssi
        self.service_uuids = list(service_uuids)
        self.manufacturer_data = manufacturer_data or {}
        self.service_data = service_data or {}
        self.tx_power = tx_power


LEGACY_ADVERTISEMENT = bytes.fromhex("02a2000304015c013bdbb8a20157")


class IOSenseProbeDiscoveryTests(unittest.TestCase):
    def test_parser_keeps_charger_mode_as_default(self):
        args = tool.build_parser().parse_args([])
        self.assertFalse(args.brush_pacer)
        self.assertFalse(args.scan_only)
        self.assertFalse(args.no_protocol)
        self.assertEqual(240.0, args.session_timeout)
        self.assertTrue(str(tool.default_output_path()).startswith("iosense-probe-"))

    def test_legacy_advertisement_decodes(self):
        value = tool.decode_legacy_advertisement(LEGACY_ADVERTISEMENT)
        self.assertTrue(value["compatible"])
        self.assertEqual("stargate", value["device_type"])
        self.assertEqual("0.3.4", value["firmware"])
        self.assertEqual("5C:01:3B:DB:B8:A2", value["advertised_mac"])
        self.assertTrue(value["status"]["brush_paired"])

    def test_changed_payload_remains_visible(self):
        value = tool.decode_legacy_advertisement(bytes.fromhex("03a3010203040506"))
        self.assertFalse(value["compatible"])
        self.assertEqual(8, value["length"])
        self.assertEqual(0xA3, value["device_type_byte"])

    def test_known_charger_scores_above_toothbrush(self):
        charger_score, charger_reasons = tool.candidate_score(
            "iO Sense",
            [tool.IOSENSE_SERVICE_UUID.upper()],
            {tool.ORALB_MANUFACTURER_ID: LEGACY_ADVERTISEMENT},
            -50,
        )
        brush_score, brush_reasons = tool.candidate_score(
            "iO Series",
            [],
            {tool.ORALB_MANUFACTURER_ID: b"\x00" * 11},
            -35,
        )
        self.assertGreater(charger_score, brush_score)
        self.assertIn("legacy_stargate_payload", charger_reasons)
        self.assertIn("toothbrush_11_byte_payload", brush_reasons)

    def test_unknown_oralb_payload_is_candidate(self):
        score, reasons = tool.candidate_score(
            "Unknown", [], {tool.ORALB_MANUFACTURER_ID: b"\x03\xa3" + b"\x00" * 16}, -70
        )
        self.assertGreaterEqual(score, 40)
        self.assertIn("unknown_oralb_payload_18_bytes", reasons)

    def test_build_candidates_and_report(self):
        charger_device = FakeDevice("AA:BB:CC:DD:EE:FF")
        charger_advertisement = FakeAdvertisement(
            local_name="iO Sense",
            rssi=-48,
            service_uuids=[tool.IOSENSE_SERVICE_UUID],
            manufacturer_data={tool.ORALB_MANUFACTURER_ID: LEGACY_ADVERTISEMENT},
            tx_power=-4,
        )
        other_device = FakeDevice("11:22:33:44:55:66", "Other")
        other_advertisement = FakeAdvertisement(rssi=-20)
        candidates = tool.build_candidates(
            {
                charger_device.address: (charger_device, charger_advertisement),
                other_device.address: (other_device, other_advertisement),
            }
        )
        self.assertEqual(charger_device.address, candidates[0].address)
        self.assertEqual(
            candidates[0],
            tool.choose_candidate(candidates, None, interactive=False),
        )
        report = tool.advertisement_report(candidates[0])
        self.assertEqual(-48, report["rssi"])
        self.assertEqual(
            LEGACY_ADVERTISEMENT.hex(),
            report["manufacturer_data"][str(tool.ORALB_MANUFACTURER_ID)]["hex"],
        )
        self.assertTrue(report["oralb_advertisement"]["compatible"])

    def test_ambiguous_noninteractive_selection_requires_address(self):
        candidates = [
            tool.Candidate(
                FakeDevice(f"address-{index}"),
                FakeAdvertisement(
                    local_name="iO Sense", service_uuids=[tool.IOSENSE_SERVICE_UUID]
                ),
                100,
                ("known_iosense_service_uuid",),
            )
            for index in range(2)
        ]
        with self.assertRaisesRegex(RuntimeError, "ambiguous"):
            tool.choose_candidate(candidates, None, interactive=False)
        self.assertEqual(
            "address-1",
            tool.choose_candidate(candidates, "ADDRESS-1", interactive=False).address,
        )

    def test_toothbrush_selection_uses_11_byte_payload_not_charger_score(self):
        charger = tool.Candidate(
            FakeDevice("charger"),
            FakeAdvertisement(
                local_name="iO Sense",
                manufacturer_data={
                    tool.ORALB_MANUFACTURER_ID: LEGACY_ADVERTISEMENT
                },
            ),
            200,
            ("legacy_stargate_payload",),
        )
        brush = tool.Candidate(
            FakeDevice("brush"),
            FakeAdvertisement(
                local_name="Oral-B iO",
                manufacturer_data={tool.ORALB_MANUFACTURER_ID: b"\x00" * 11},
            ),
            -90,
            ("toothbrush_11_byte_payload",),
        )
        selected = tool.choose_toothbrush_candidate(
            [charger, brush], None, interactive=False
        )
        self.assertEqual("brush", selected.address)

    def test_toothbrush_address_override_remains_exact(self):
        candidate = tool.Candidate(
            FakeDevice("explicit-device"), FakeAdvertisement(), 0, ()
        )
        selected = tool.choose_toothbrush_candidate(
            [candidate], "EXPLICIT-DEVICE", interactive=False
        )
        self.assertEqual(candidate, selected)

    def test_toothbrush_name_can_select_when_payload_is_unavailable(self):
        candidate = tool.Candidate(
            FakeDevice("brush"),
            FakeAdvertisement(local_name="Oral-B iO"),
            0,
            (),
        )
        self.assertTrue(tool.is_toothbrush_candidate(candidate))


class FakeProtocolClient:
    """In-memory charger transport that accepts GET frames only."""

    def __init__(self) -> None:
        self.callbacks = {}
        self.writes = []
        self.command = None

    async def start_notify(self, uuid, callback):
        self.callbacks[uuid] = callback

    async def stop_notify(self, uuid):
        self.callbacks.pop(uuid, None)

    async def write_gatt_char(self, uuid, value, response):
        raw = bytes(value)
        self.writes.append((uuid, raw, response))
        if raw != tool.IOSENSE_PROTOCOL_END:
            operation, self.command = raw
            if operation != tool.GET_OPERATION:
                raise AssertionError("probe sent a non-GET operation")
            return
        payloads = {
            0x1E: bytes((1, 2, 3)),
            0x25: bytes((4,)),
        }
        payload = payloads.get(self.command, b"")
        self.callbacks[tool.IOSENSE_READ_UUID](
            None,
            bytearray((self.command, tool.GET_OPERATION)) + bytearray(payload),
        )


class IOSenseProbeProtocolTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.client = FakeProtocolClient()
        self.probe = tool.ReadOnlyProtocolProbe(
            self.client, request_timeout=0.1, frame_delay=0
        )
        await self.probe.start()

    async def asyncTearDown(self):
        await self.probe.stop()

    async def test_get_probe_decodes_and_never_sends_post(self):
        firmware = await self.probe.get(0x1E)
        hardware = await self.probe.get(0x25)
        self.assertEqual("1.2.3", firmware["value"])
        self.assertEqual(4, hardware["value"])

        self.assertTrue(all(response for _uuid, _raw, response in self.client.writes))
        for uuid, raw, _response in self.client.writes:
            self.assertEqual(tool.IOSENSE_COMMAND_UUID, uuid)
            self.assertTrue(
                raw == tool.IOSENSE_PROTOCOL_END or raw[0] == tool.GET_OPERATION
            )

    async def test_brush_data_decoder(self):
        payload = bytearray(41)
        payload[1:7] = bytes.fromhex("58263af664d3")
        payload[16] = 0x36
        payload[39] = 0x08
        payload[40] = 0x52
        value = tool.decode_probe_value(0x39, bytes(payload))
        self.assertEqual("58:26:3A:F6:64:D3", value["brush_mac"])
        self.assertEqual(0x36, value["model_id"])
        self.assertEqual(0x08, value["protocol_version"])
        self.assertEqual(0x52, value["firmware_revision"])


class FakeDescriptor:
    def __init__(self, uuid: str, handle: int) -> None:
        self.uuid = uuid
        self.handle = handle
        self.description = "test descriptor"


class FakeCharacteristic:
    def __init__(self, uuid: str, properties, descriptors=()) -> None:
        self.uuid = uuid
        self.handle = 10
        self.description = "test characteristic"
        self.properties = list(properties)
        self.descriptors = list(descriptors)


class FakeService:
    def __init__(self, characteristics) -> None:
        self.uuid = tool.IOSENSE_SERVICE_UUID
        self.handle = 1
        self.description = "test service"
        self.characteristics = list(characteristics)


class FakeGattClient:
    def __init__(self, services) -> None:
        self.services = list(services)
        self.characteristic_reads = []
        self.descriptor_reads = []

    async def read_gatt_char(self, characteristic):
        self.characteristic_reads.append(characteristic.uuid)
        return b"\x01\x02"

    async def read_gatt_descriptor(self, handle):
        self.descriptor_reads.append(handle)
        return b"\x03"


class IOSenseProbeGattTests(unittest.IsolatedAsyncioTestCase):
    async def test_gatt_dump_reads_only_readable_characteristics(self):
        descriptor = FakeDescriptor("00002902-0000-1000-8000-00805f9b34fb", 12)
        readable = FakeCharacteristic(
            tool.IOSENSE_READ_UUID, ["read", "notify"], [descriptor]
        )
        write_only = FakeCharacteristic(tool.IOSENSE_WRITE_UUID, ["write"])
        client = FakeGattClient([FakeService([readable, write_only])])

        services, characteristic_uuids = await tool.capture_gatt(
            client, read_timeout=0.1
        )

        self.assertEqual([tool.IOSENSE_READ_UUID], client.characteristic_reads)
        self.assertEqual([12], client.descriptor_reads)
        self.assertEqual(
            {tool.IOSENSE_READ_UUID, tool.IOSENSE_WRITE_UUID}, characteristic_uuids
        )
        self.assertEqual("0102", services[0]["characteristics"][0]["read"]["hex"])
        self.assertEqual(
            "03",
            services[0]["characteristics"][0]["descriptors"][0]["read"]["hex"],
        )


class FakeBrushClient:
    def __init__(self, *, sector_subscription_error=False) -> None:
        self.callbacks = {}
        self.stopped = []
        self.characteristic_writes = []
        self.sector_subscription_error = sector_subscription_error
        self.read_values = {
            tool.BRUSH_DEVICE_INFO_UUID: b"\x36\x06\x32",
            tool.BRUSH_AVAILABLE_MODES_UUID: b"\x00\x01\x02",
            tool.BRUSH_PACER_UUID: b"",
            tool.BRUSH_SECTOR_UUID: b"\x00\x00\x00",
        }

    async def start_notify(self, uuid, callback):
        if self.sector_subscription_error and uuid == tool.BRUSH_SECTOR_UUID:
            raise RuntimeError("sector unavailable")
        self.callbacks[uuid] = callback

    async def stop_notify(self, uuid):
        self.stopped.append(uuid)
        self.callbacks.pop(uuid, None)

    async def read_gatt_char(self, uuid):
        return self.read_values[uuid]

    async def write_gatt_char(self, uuid, value, response=True):
        self.characteristic_writes.append((uuid, bytes(value), response))

    def notify(self, uuid, value):
        self.callbacks[uuid](None, bytearray(value))


class BrushPacerCaptureTests(unittest.IsolatedAsyncioTestCase):
    async def test_read_preserves_empty_and_all_zero_values(self):
        client = FakeBrushClient()
        empty = await tool.read_characteristic(
            client, tool.BRUSH_PACER_UUID, read_timeout=0.1
        )
        zeros = await tool.read_characteristic(
            client, tool.BRUSH_SECTOR_UUID, read_timeout=0.1
        )
        self.assertEqual({"success": True, "length": 0, "hex": ""}, empty)
        self.assertEqual(
            {"success": True, "length": 3, "hex": "000000"}, zeros
        )

        tool.annotate_brush_read("pacer_configuration_ff26", empty)
        tool.annotate_brush_read("sector_ff09", zeros)
        self.assertEqual([], empty["positional_hints"]["usable_sector_seconds"])
        self.assertEqual(0, zeros["positional_hints"]["total_hint_raw"])

    async def test_snapshot_includes_ff25_and_pre_post_configuration_reads(self):
        client = FakeBrushClient()
        initial = await tool.read_brush_snapshot(
            client, tool.BRUSH_INITIAL_READS, read_timeout=0.1
        )
        final = await tool.read_brush_snapshot(
            client, tool.BRUSH_FINAL_READS, read_timeout=0.1
        )

        self.assertEqual(
            {
                "device_info_ff02",
                "available_modes_ff25",
                "pacer_configuration_ff26",
                "sector_ff09",
            },
            set(initial),
        )
        self.assertEqual(
            {
                "available_modes_ff25",
                "pacer_configuration_ff26",
                "sector_ff09",
            },
            set(final),
        )
        self.assertEqual(
            [0, 1, 2],
            initial["available_modes_ff25"]["positional_hints"]["mode_values_raw"],
        )
        self.assertEqual(
            6,
            initial["device_info_ff02"]["positional_hints"][
                "protocol_version_raw"
            ],
        )
        self.assertEqual([], client.characteristic_writes)

    async def test_records_raw_notifications_and_stops_after_running(self):
        client = FakeBrushClient()
        capture = tool.BrushPacerCapture(client)
        await capture.start()

        client.notify(tool.BRUSH_STATE_UUID, b"\x03")
        client.notify(tool.BRUSH_MODE_UUID, b"\x05")
        client.notify(tool.BRUSH_TIME_UUID, b"\x01\x02")
        client.notify(tool.BRUSH_SECTOR_UUID, b"\x04\x09\x10")
        client.notify(tool.BRUSH_STATE_UUID, b"\x02")

        reason = await capture.wait(session_timeout=0.1, end_grace=0)
        report = capture.report(reason)
        await capture.stop()

        self.assertEqual("state_after_running", reason)
        self.assertTrue(report["running_seen"])
        self.assertEqual(2, report["end_state_raw"])
        self.assertEqual(
            ["03", "05", "0102", "040910", "02"],
            [item["hex"] for item in report["notifications"]],
        )
        self.assertEqual(62, report["notifications"][2]["brushing_time_seconds_hint"])
        self.assertEqual(4, report["notifications"][3]["sector_raw"])
        self.assertEqual([], client.characteristic_writes)
        self.assertEqual(
            {uuid for _name, uuid in tool.BRUSH_NOTIFY_CHARACTERISTICS},
            set(client.stopped),
        )

    async def test_idle_before_running_does_not_end_capture(self):
        client = FakeBrushClient()
        capture = tool.BrushPacerCapture(client)
        await capture.start()
        client.notify(tool.BRUSH_STATE_UUID, b"\x02")

        reason = await capture.wait(session_timeout=0.01, end_grace=0)
        await capture.stop()

        self.assertEqual("timeout", reason)
        self.assertFalse(capture.running_seen)

    async def test_ff09_subscription_is_required_and_partial_setup_cleans_up(self):
        client = FakeBrushClient(sector_subscription_error=True)
        capture = tool.BrushPacerCapture(client)
        with self.assertRaisesRegex(RuntimeError, "required.*FF09"):
            await capture.start()

        await capture.stop()

        self.assertIn("sector", capture.subscription_errors)
        self.assertNotIn(tool.BRUSH_SECTOR_UUID, capture.subscribed)
        self.assertEqual(set(capture.subscribed), set(client.stopped))
        self.assertEqual([], client.characteristic_writes)


if __name__ == "__main__":
    unittest.main()
