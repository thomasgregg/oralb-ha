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


if __name__ == "__main__":
    unittest.main()
