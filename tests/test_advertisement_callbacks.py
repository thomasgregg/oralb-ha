"""Issue #53 regressions using the real Home Assistant Bluetooth manager."""

from __future__ import annotations

import unittest
from functools import partial
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import habluetooth.manager as bluetooth_manager
from bleak.backends.device import BLEDevice
from bleak.backends.scanner import AdvertisementData
from habluetooth import BluetoothServiceInfoBleak
from homeassistant.components import bluetooth
from homeassistant.components.bluetooth import api

from tests.test_battery_refresh import ADDRESS, const
from tests.test_post_session_beta import brush

from oralb_live.charger_protocol import IOSENSE_SERVICE_UUID


OTHER_BRUSH = "AA:BB:CC:DD:EE:01"
CHARGER = "AA:BB:CC:DD:EE:02"
OTHER_CHARGER = "AA:BB:CC:DD:EE:03"


def service_info(
    address, timestamp, payload, *, connectable=False, service_uuids=(), rssi=-60
):
    source = "connectable_proxy" if connectable else "advertisement_only_proxy"
    manufacturer_data = {} if payload is None else {const.ORALB_MANUFACTURER_ID: payload}
    service_uuids = list(service_uuids)
    try:
        device = BLEDevice(address, "Oral-B", {"source": source})
    except TypeError:
        device = BLEDevice(address, "Oral-B", {"source": source}, rssi)
    advertisement = AdvertisementData(
        "Oral-B", manufacturer_data, {}, service_uuids, None, rssi, ()
    )
    args = (
        "Oral-B", address, rssi, manufacturer_data, {}, service_uuids,
        source, device, advertisement, connectable, timestamp,
    )
    try:
        return BluetoothServiceInfoBleak(*args, tx_power=None)
    except TypeError:
        return BluetoothServiceInfoBleak(*args)


class AdvertisementCallbackTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        register = bluetooth.async_register_callback
        h = self.h = await self.enterAsyncContext(brush())
        h.manager._loop = h.hass.loop
        self.seen = []
        self.lookups = []
        self.enterContext(patch.object(api, "_get_manager", return_value=h.manager))
        self.enterContext(
            patch.object(
                bluetooth, "async_register_callback",
                side_effect=partial(self.register, register),
            )
        )
        self.enterContext(
            patch.object(
                bluetooth, "async_ble_device_from_address", side_effect=self.lookup
            )
        )
        self.enterContext(
            patch.object(
                bluetooth_manager, "monotonic_time_coarse", side_effect=lambda: h.clock.now
            )
        )

    def register(self, register, hass, callback, matcher, mode):
        def receive(info, change):
            self.seen.append((matcher.copy(), info))
            callback(info, change)

        return register(hass, receive, matcher, mode)

    def lookup(self, _hass, address, connectable=True):
        device = self.h.manager.async_ble_device_from_address(address, connectable)
        self.lookups.append((address, connectable, device))
        return device

    async def send(self, info):
        self.h.manager.scanner_adv_received(info)
        await self.h._ready()
        return info

    async def send_brush(
        self, state, seconds=0, *, press=0, sector=1, connectable=False,
        address=ADDRESS, rssi=-60,
    ):
        payload = bytes(
            (7, 0x31, 36, state, press, seconds // 60, seconds % 60, 0, sector, 0, 4)
        )
        return await self.send(
            service_info(address, self.h.clock.now, payload, connectable=connectable, rssi=rssi)
        )

    async def send_charger(
        self, bits=0x40, *, connectable=False, address=CHARGER,
        service_uuids=(IOSENSE_SERVICE_UUID,),
    ):
        payload = bytes.fromhex("01 a2 01 02 03 00 aa bb cc dd ee 02 00") + bytes((bits,))
        return await self.send(
            service_info(
                address, self.h.clock.now, payload,
                connectable=connectable, service_uuids=service_uuids,
            )
        )

    def charger_transport(self, *, paired_brush=ADDRESS):
        bridge = self.h.c.charger
        client = MagicMock(is_connected=True)
        client.start_notify = AsyncMock()
        client.disconnect = AsyncMock()
        identity = SimpleNamespace(value={"brush_mac": paired_brush})
        self.enterContext(patch.object(bridge, "_async_get", new=AsyncMock(return_value=identity)))
        self.enterContext(patch.object(bridge, "_async_snapshot", new=AsyncMock()))
        return self.enterContext(
            patch("oralb_live.charger.establish_connection", new=AsyncMock(return_value=client))
        )

    def brush_callbacks(self):
        return [info for matcher, info in self.seen if matcher.get("address") == ADDRESS]

    async def test_passive_cached_running_is_displayed_then_changed_frames_update(self):
        h = self.h
        await self.send_brush(3, 15)
        h.c.async_start()
        self.assertEqual(h.c.data["state"], "running")
        self.assertEqual(h.c.data["time"], 15)
        self.assertFalse(h.c._session_active)
        self.assertFalse(h.c._fresh_advertisement_seen)

        await h._advance(31)
        await self.send_brush(3, 46, press=0x80, sector=2)
        self.assertEqual(h.c.data["time"], 46)
        self.assertEqual(h.c.data["pressure"], "high")
        self.assertEqual(h.c.data["sector"], "sector_2")
        self.assertEqual(h.publications[-1]["time"], 46)
        self.assertEqual(h.c.data["data_source"], const.DATA_SOURCE_ADVERTISEMENT)

        await h._advance(74)
        await self.send_brush(9, 120, sector=4)
        await h._advance(20)
        self.assertEqual(h.c.data["state"], "session_summary")
        self.assertEqual(h.c.data["time"], 120)
        self.assertIsNone(h.c.data["pressure"])
        self.assertEqual(h.c.data["sector"], "no_sector")
        self.assertEqual(h.c.data["last_session_duration"], 120)
        self.assertEqual(h.c.data["sessions_today"], 1)
        self.assertEqual(h.connections, [])

    async def test_passive_first_packet_after_start_without_history(self):
        h = self.h
        h.c.async_start()
        self.assertFalse(h.c.available)
        await self.send_brush(3, 0)
        self.assertTrue(h.c.available)
        self.assertEqual(h.c.data["state"], "running")

    async def test_unchanged_passive_retransmission_does_not_duplicate_callback(self):
        h = self.h
        h.c.async_start()
        await self.send_brush(3, 10)
        await h._advance(1)
        await self.send_brush(3, 10)
        await h._advance(1)
        await self.send_brush(3, 10, press=0x80)
        self.assertEqual(len(self.brush_callbacks()), 2)
        self.assertEqual(h.c.data["pressure"], "high")

    async def test_cached_running_identical_repeat_does_not_start_session(self):
        h = self.h
        await self.send_brush(3, 100)
        h.c.async_start()
        await h._advance(1)
        await self.send_brush(3, 100)
        self.assertFalse(h.c._fresh_advertisement_seen)
        self.assertFalse(h.c._session_active)
        self.assertIsNone(h.c.data["last_session_duration"])
        self.assertEqual(h.connections, [])

    async def test_cached_passive_quiet_packet_does_not_authorize_sync(self):
        h = self.h
        await self.send_brush(4)
        h.c.async_start()
        self.assertEqual(h.c.data["state"], "charging")
        self.assertFalse(h.c._fresh_advertisement_seen)
        await h._advance(90)
        self.assertEqual(h.connections, [])
        self.assertEqual(h.c.data["battery"], 99)

    async def test_malformed_or_wrong_address_passive_frame_cannot_change_values(self):
        h = self.h
        await self.send_brush(3, 15)
        h.c.async_start()
        await self.send_brush(9, 120, address=OTHER_BRUSH)
        await self.send(service_info(ADDRESS, h.clock.now, None))
        await self.send(service_info(ADDRESS, h.clock.now, b"\x07\x31"))
        self.assertEqual(h.c.data["state"], "running")
        self.assertEqual(h.c.data["time"], 15)
        self.assertFalse(h.c._session_active)

    async def test_passive_disappearance_and_reappearance(self):
        h = self.h
        h.c.async_start()
        await self.send_brush(8, 12)
        self.assertTrue(h.c.available)
        h.clock.now += 86400
        h.manager._async_check_unavailable()
        self.assertFalse(h.c.available)
        self.assertFalse(h.c._session_active)
        await self.send_brush(3, 0)
        self.assertTrue(h.c.available)
        self.assertEqual(h.c.data["time"], 0)

    async def test_unload_unsubscribes_from_passive_stream(self):
        h = self.h
        h.c.async_start()
        await self.send_brush(3, 10)
        self.assertEqual(h.c.data["time"], 10)
        await h.c.async_stop()
        notifications = len(self.seen)
        await self.send_brush(3, 11)
        self.assertEqual(len(self.seen), notifications)
        self.assertEqual(h.c.data["time"], 10)

    async def test_connectable_advertisements_and_battery_sync_still_work(self):
        h = self.h
        h.c.async_start()
        packet = await self.send_brush(4, connectable=True)
        await h._advance(1)
        self.assertEqual(h.c.data["state"], "charging")
        self.assertEqual(h.c.data["battery"], 23)
        self.assertEqual(self.lookups, [(ADDRESS, True, packet.device)])

    async def test_passive_quiet_frame_does_not_create_gatt_route(self):
        h = self.h
        h.c.async_start()
        await self.send_brush(4)
        self.assertEqual(h.c.data["state"], "charging")
        await h._advance(90)
        self.assertIsNone(h.manager.async_ble_device_from_address(ADDRESS, True))
        self.assertEqual(h.connections, [])
        self.assertEqual(h.c.data["battery"], 99)

    async def test_mixed_sources_select_connectable_gatt_device(self):
        h = self.h
        h.c.async_start()
        passive = await self.send_brush(4)
        await h._advance(1)
        self.assertEqual(h.connections, [])
        connectable = await self.send_brush(4, connectable=True)
        await h._advance(1)
        self.assertIsNot(passive.device, connectable.device)
        self.assertEqual(self.lookups, [(ADDRESS, True, connectable.device)])
        self.assertEqual(h.c.data["battery"], 23)

    async def test_passive_frame_does_not_override_charger_owned_brush_state(self):
        h = self.h
        h.c.async_start()
        h.c._charger_session_started(confirmed=True)
        await self.send_brush(8, 61)
        self.assertEqual(len(self.brush_callbacks()), 1)
        self.assertEqual(h.c.data["state"], "running")
        self.assertEqual(h.c.data["time"], 0)
        self.assertEqual(h.c.data["data_source"], const.DATA_SOURCE_CHARGER)

    async def test_passive_frame_does_not_supply_a_direct_mode_connection(self):
        h = self.h
        h.c.mode = const.CONNECTION_MODE_LIVE
        h.c.charger = None
        h.c.async_start()
        await self.send_brush(3, 12)
        self.assertEqual(h.c.data["state"], "running")
        self.assertEqual(h.c.data["time"], 12)
        self.assertFalse(h.c.data["live"])
        self.assertIn((ADDRESS, True, None), self.lookups)
        self.assertEqual(h.connections, [])

    async def test_delayed_passive_frame_cannot_end_direct_gatt_session(self):
        h = self.h
        h.c.mode = const.CONNECTION_MODE_LIVE
        h.c.charger = None
        h.c.async_start()
        await self.send_brush(3, 0, connectable=True)
        self.assertTrue(h.c.data["live"])
        h.c._on_notify(SimpleNamespace(uuid=const.CHAR_STATE), bytearray([3]))
        await h._advance(1)
        h.c._on_notify(SimpleNamespace(uuid=const.CHAR_BRUSH_TIME), bytearray([0, 1]))
        h.c._on_notify(SimpleNamespace(uuid=const.CHAR_SECTOR), bytearray([1, 12, 4]))
        h.c._on_notify(SimpleNamespace(uuid=const.CHAR_PRESSURE), bytearray([1]))
        direct_sample = {
            key: h.c.data[key] for key in ("state", "time", "sector", "pressure")
        }
        self.assertEqual(direct_sample["state"], "running")
        self.assertEqual(direct_sample["sector"], "sector_2")
        self.assertIsNotNone(direct_sample["pressure"])
        self.assertTrue(h.c._session_active)

        # The brush stops advertising while GATT owns its slot. Its
        # connectable advertisement can expire while GATT remains connected.
        h.manager._async_check_unavailable()
        self.assertIsNone(h.manager.async_ble_device_from_address(ADDRESS, True))
        await self.send_brush(2, 0, rssi=-72)
        self.assertTrue(h.c.data["live"])
        self.assertEqual({key: h.c.data[key] for key in direct_sample}, direct_sample)
        self.assertTrue(h.c._session_active)
        self.assertEqual(h.c.data["rssi"], -72)

        h.c._on_notify(SimpleNamespace(uuid=const.CHAR_STATE), bytearray([2]))
        self.assertEqual(h.c.data["state"], "idle")
        self.assertFalse(h.c._session_active)

    async def test_passive_fallback_resumes_after_direct_disconnect(self):
        h = self.h
        h.c.mode = const.CONNECTION_MODE_LIVE
        h.c.charger = None
        h.c.async_start()
        await self.send_brush(3, 0, connectable=True)
        self.assertTrue(h.c.data["live"])
        h.manager._async_check_unavailable()
        await h.c._async_disconnect()
        await self.send_brush(3, 11, press=0x80)
        self.assertFalse(h.c.data["live"])
        self.assertEqual(h.c.data["data_source"], const.DATA_SOURCE_ADVERTISEMENT)
        self.assertEqual(h.c.data["time"], 11)
        self.assertEqual(h.c.data["pressure"], "high")

    async def test_passive_pause_and_resume_counts_one_session(self):
        h = self.h
        h.c.async_start()
        await self.send_brush(3, 0)
        await h._advance(30)
        await self.send_brush(9, 30)
        await h._advance(5)
        await self.send_brush(3, 30)
        await h._advance(1)
        await self.send_brush(3, 31)
        await h._advance(89)
        await self.send_brush(10, 120)
        await h._advance(20)
        self.assertEqual(h.c.data["sessions_today"], 1)
        self.assertEqual(h.c.data["last_session_duration"], 120)
        self.assertEqual(h.connections, [])

    async def test_connectable_charger_is_matched_via_brush_identity(self):
        h = self.h
        establish = self.charger_transport()
        h.c.async_start()
        packet = await self.send_charger(connectable=True)
        self.assertEqual(h.c.charger.address, CHARGER)
        self.assertEqual(h.c.data["charger_address"], CHARGER)
        self.assertIs(establish.await_args.args[1], packet.device)
        self.assertIn((CHARGER, True, packet.device), self.lookups)
        await self.send_charger(connectable=True, address=OTHER_CHARGER)
        self.assertEqual(h.c.charger.data["address"], CHARGER)
        establish.assert_awaited_once()

    async def test_charger_wrong_paired_brush_is_not_matched(self):
        h = self.h
        establish = self.charger_transport(paired_brush=OTHER_BRUSH)
        h.c.async_start()
        await self.send_charger(connectable=True)
        establish.assert_awaited_once()
        self.assertIsNone(h.c.charger.address)
        self.assertIsNone(h.c.data["charger_address"])

    async def test_charger_requires_service_uuid(self):
        h = self.h
        establish = self.charger_transport()
        h.c.async_start()
        await self.send_charger(connectable=True, service_uuids=())
        establish.assert_not_awaited()
        self.assertFalse(h.c.charger.available)

    async def test_passive_only_charger_does_not_make_absent_brush_available(self):
        h = self.h
        establish = self.charger_transport()
        await self.send_charger()  # Also exercise service-UUID replay at startup.
        h.c.async_start()
        await self.send_charger(0x50)
        h.c._push()
        establish.assert_not_awaited()
        self.assertIsNone(h.c.data["charger_address"])
        self.assertFalse(h.c.available)
        self.assertFalse(h.c.charger.available)
        self.assertEqual(self.lookups, [])

    async def test_passive_charger_frames_do_not_mask_loss_of_connectable_route(self):
        h = self.h
        establish = self.charger_transport()
        h.c.async_start()
        await self.send_charger(connectable=True)
        self.assertEqual(h.c.charger.address, CHARGER)
        self.assertTrue(h.c.charger.available)
        # The manager has history but no scanner claiming the address. A
        # connectable route expires immediately on this check. All-source
        # history stays fresh, matching the passive-only handoff.
        await h._advance(1)
        h.manager._async_check_unavailable()
        self.assertFalse(h.c.charger.available)
        self.assertIsNone(h.manager.async_ble_device_from_address(CHARGER, True))
        await self.send_charger(0x50)
        establish.assert_awaited_once()
        self.assertFalse(h.c.charger.available)
        self.assertEqual(h.c.charger.data["state"], "unavailable")
