"""An iO Sense that could not be identified must be retried.

Home Assistant dispatches only changed advertisements. A charger that was
skipped while another one was being identified, or whose identification
failed, would otherwise wait for its next state change, which is usually the
next brushing session, so the brush ran without its charger bridge for hours
after a Home Assistant restart in a household with two iO Sense chargers.
"""

import asyncio
import contextlib
import importlib
import pathlib
import sys
import types
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

_PACKAGE = types.ModuleType("oralb_live")
_PACKAGE.__path__ = [
    str(pathlib.Path(__file__).parents[1] / "custom_components/oralb_live")
]
sys.modules.setdefault("oralb_live", _PACKAGE)
coordinator = importlib.import_module("oralb_live.coordinator")
charger = importlib.import_module("oralb_live.charger")

OWN_BRUSH = "AA:BB:CC:DD:EE:FF"
OTHER_BRUSH = "11:22:33:44:55:66"
OWN_CHARGER = "C0:5D:89:E7:96:92"
OTHER_CHARGER = "70:4B:CA:50:18:46"


def advert(address):
    """A real iO Sense value: stargate, Wi-Fi and internet up, brush paired, idle."""
    mac = bytes.fromhex(address.replace(":", ""))
    payload = bytes((0x02, 0xA2, 0x00, 0x03, 0x04, 0x02)) + mac + bytes((0x01, 0x43))
    return SimpleNamespace(
        address=address,
        rssi=-88,
        manufacturer_data={charger.ORALB_MANUFACTURER_ID: payload},
    )


class FakeClient:
    def __init__(self, address):
        self.address = address
        self.is_connected = True
        self.start_notify = AsyncMock()
        self.write_gatt_char = AsyncMock()
        self.disconnect = AsyncMock(side_effect=self._disconnect)

    async def _disconnect(self):
        self.is_connected = False


def make_bridge():
    hass = MagicMock()
    hass.loop = asyncio.get_running_loop()
    tasks = []

    def create_task(coro, *args, **kwargs):
        task = asyncio.create_task(coro)
        tasks.append(task)
        return task

    hass.async_create_task.side_effect = create_task
    hass.async_create_background_task.side_effect = create_task
    parent = coordinator.OralBLiveCoordinator(
        hass, OWN_BRUSH, "test", coordinator.CONNECTION_MODE_CHARGER
    )
    parent._push = MagicMock()
    parent._charger_discovered = MagicMock()
    bridge = charger.IOSenseBridge(parent)
    bridge._push = MagicMock()
    return bridge, tasks


def identity_replies(bridge, replies):
    """Answer the paired-brush request per charger; None is a failed read."""

    async def read(command):
        if command is not charger.ChargerCommand.BRUSH_DATA:
            return None
        value = replies[bridge._client.address].pop(0)
        return None if value is None else SimpleNamespace(value={"brush_mac": value})

    return AsyncMock(side_effect=read)


@contextlib.contextmanager
def radio(establish, base=0.02, maximum=0.05):
    with contextlib.ExitStack() as stack:
        stack.enter_context(
            patch.object(
                charger.bluetooth,
                "async_ble_device_from_address",
                side_effect=lambda hass, address, connectable=True: SimpleNamespace(
                    address=address
                ),
            )
        )
        stack.enter_context(patch.object(charger, "establish_connection", establish))
        stack.enter_context(
            patch.object(
                charger.bluetooth, "async_track_unavailable", return_value=lambda: None
            )
        )
        stack.enter_context(patch.object(charger, "async_dispatcher_send"))
        for name, seconds in (
            ("CHARGER_DISCOVERY_RETRY_BUSY_SECONDS", 0.01),
            ("CHARGER_DISCOVERY_RETRY_BASE_SECONDS", base),
            ("CHARGER_DISCOVERY_RETRY_MAX_SECONDS", maximum),
        ):
            stack.enter_context(patch.object(charger, name, seconds, create=True))
        yield


def connects():
    return AsyncMock(
        side_effect=lambda cls, device, name, **kwargs: FakeClient(device.address)
    )


async def settle(bridge, seconds=1.0):
    loop = asyncio.get_running_loop()
    deadline = loop.time() + seconds
    while loop.time() < deadline:
        await asyncio.sleep(0.01)
        task = bridge._connect_task
        if bridge.address is not None and (task is None or task.done()):
            return


def test_charger_advertised_while_another_is_identified_is_retried():
    async def exercise():
        bridge, _ = make_bridge()
        bridge._async_get = identity_replies(
            bridge, {OTHER_CHARGER: [OTHER_BRUSH], OWN_CHARGER: [OWN_BRUSH]}
        )
        with radio(connects()):
            # Both chargers are replayed at setup, in the same loop iteration.
            bridge._async_advertisement(advert(OTHER_CHARGER), None)
            bridge._async_advertisement(advert(OWN_CHARGER), None)
            await settle(bridge)
            assert bridge.address == OWN_CHARGER
            await bridge.async_stop()

    asyncio.run(exercise())


def test_failed_paired_brush_request_is_retried():
    async def exercise():
        bridge, _ = make_bridge()
        bridge._async_get = identity_replies(bridge, {OWN_CHARGER: [None, OWN_BRUSH]})
        establish = connects()
        with radio(establish):
            bridge._async_advertisement(advert(OWN_CHARGER), None)
            await settle(bridge)
            assert bridge.address == OWN_CHARGER
            assert establish.await_count == 2
            await bridge.async_stop()

    asyncio.run(exercise())


def test_failed_connection_is_retried_without_a_new_advertisement():
    async def exercise():
        bridge, _ = make_bridge()
        bridge._async_get = identity_replies(bridge, {OWN_CHARGER: [OWN_BRUSH]})
        attempts = []

        def establish(cls, device, name, **kwargs):
            attempts.append(device.address)
            if len(attempts) == 1:
                raise charger.BleakError("weak signal")
            return FakeClient(device.address)

        with radio(AsyncMock(side_effect=establish)):
            bridge._async_advertisement(advert(OWN_CHARGER), None)
            await settle(bridge)
            assert bridge.address == OWN_CHARGER
            assert attempts == [OWN_CHARGER, OWN_CHARGER]
            await bridge.async_stop()

    asyncio.run(exercise())


def test_charger_of_another_brush_is_not_retried():
    async def exercise():
        bridge, _ = make_bridge()
        bridge._async_get = identity_replies(bridge, {OTHER_CHARGER: [OTHER_BRUSH]})
        establish = connects()
        with radio(establish):
            bridge._async_advertisement(advert(OTHER_CHARGER), None)
            await asyncio.sleep(0.3)
            bridge._async_advertisement(advert(OTHER_CHARGER), None)
            await asyncio.sleep(0.1)
            assert bridge.address is None
            assert establish.await_count == 1
            await bridge.async_stop()

    asyncio.run(exercise())


def test_no_retry_after_stop():
    async def exercise():
        bridge, _ = make_bridge()
        bridge._async_get = identity_replies(bridge, {OWN_CHARGER: [None, OWN_BRUSH]})
        establish = connects()
        with radio(establish, base=60.0, maximum=60.0):
            bridge._async_advertisement(advert(OWN_CHARGER), None)
            await bridge._connect_task
            pending = bridge._discovery_retry
            assert pending is not None
            await bridge.async_stop()
            assert pending.cancelled()
            assert bridge._discovery_retry is None
            assert establish.await_count == 1
            assert bridge.address is None

    asyncio.run(exercise())
