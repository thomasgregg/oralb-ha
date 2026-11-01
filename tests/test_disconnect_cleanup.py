"""Issue #49: disconnected BLE clients must still release backend resources.

The fake models the reported backend lifecycle: losing the radio connection
sets is_connected=False, while only disconnect() releases client resources.
This verifies integration cleanup, not actual BlueZ notification duplication.
"""

import asyncio
import importlib
import pathlib
import sys
import types
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

_PACKAGE = types.ModuleType("oralb_live")
_PACKAGE.__path__ = [
    str(pathlib.Path(__file__).parents[1] / "custom_components/oralb_live")
]
sys.modules.setdefault("oralb_live", _PACKAGE)
coordinator = importlib.import_module("oralb_live.coordinator")
charger = importlib.import_module("oralb_live.charger")


class BackendClient:
    """Keep transport connectivity separate from backend resource ownership."""

    def __init__(self, connected=False):
        self.is_connected = connected
        self.resources_open = True
        self.disconnect = AsyncMock(side_effect=self._disconnect)

    async def _disconnect(self):
        self.is_connected = False
        self.resources_open = False


def make_owner(kind):
    hass = MagicMock()
    hass.loop = asyncio.get_running_loop()
    tasks = []

    def create_task(coro, *args, **kwargs):
        task = asyncio.create_task(coro)
        tasks.append(task)
        return task

    hass.async_create_task.side_effect = create_task
    parent = coordinator.OralBLiveCoordinator(
        hass, "AA:BB:CC:DD:EE:FF", "test", coordinator.CONNECTION_MODE_LIVE
    )
    parent._push = MagicMock()
    parent._schedule_connect = MagicMock()
    owner = parent if kind == "brush" else charger.IOSenseBridge(parent)
    owner._push = MagicMock()
    return owner, tasks


async def drain_callbacks(tasks):
    # Run thread-safe callback handoffs, then await any cleanup they scheduled.
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    if tasks:
        await asyncio.gather(*tasks)


@pytest.mark.parametrize("kind", ["brush", "charger"])
@pytest.mark.parametrize(
    "connected", [False, True], ids=["already-disconnected", "connected-control"]
)
def test_explicit_disconnect_releases_backend_resources(kind, connected):
    async def exercise():
        owner, tasks = make_owner(kind)
        client = BackendClient(connected)
        owner._client = client
        await owner._async_disconnect()
        await drain_callbacks(tasks)
        client.disconnect.assert_awaited_once()
        assert not client.resources_open
        assert owner._client is None

    asyncio.run(exercise())


@pytest.mark.parametrize("kind", ["brush", "charger"])
def test_remote_disconnect_releases_old_client_resources(kind):
    async def exercise():
        owner, tasks = make_owner(kind)
        client = BackendClient()
        owner._client = client
        owner._on_disconnect(client)
        await drain_callbacks(tasks)
        client.disconnect.assert_awaited_once()
        assert not client.resources_open
        assert owner._client is None

    asyncio.run(exercise())


@pytest.mark.parametrize("kind", ["brush", "charger"])
def test_reconnect_cleans_stale_client_before_establishing_connection(kind):
    async def exercise():
        owner, tasks = make_owner(kind)
        client = BackendClient()
        owner._client = client  # Remote loss with no disconnected callback.
        module = coordinator if kind == "brush" else charger

        async def establish(*args, **kwargs):
            client.disconnect.assert_awaited_once()
            assert not client.resources_open, "old backend still open at reconnect"
            # Stop at the connection boundary; discovery/setup is unrelated.
            raise module.BleakError("simulated unavailable device")

        target = (
            patch.object(owner, "_async_establish_brush_connection", side_effect=establish)
            if kind == "brush"
            else patch.object(charger, "establish_connection", side_effect=establish)
        )
        with (
            patch.object(module.bluetooth, "async_ble_device_from_address", return_value=object()),
            target as connect,
        ):
            if kind == "brush":
                await owner._async_connect()
            else:
                await owner._async_connect("11:22:33:44:55:66")
            connect.assert_awaited_once()
        await drain_callbacks(tasks)

    asyncio.run(exercise())


@pytest.mark.parametrize("kind", ["brush", "charger"])
def test_late_old_callback_does_not_clear_replacement_connection(kind):
    async def exercise():
        owner, tasks = make_owner(kind)
        old = BackendClient()
        owner._client = old
        owner._on_disconnect(old)
        await drain_callbacks(tasks)
        replacement = BackendClient(True)
        owner._client = replacement
        if kind == "brush":
            owner._direct_available = True
            owner.data["live"] = True
        else:
            owner.data["bridge_connected"] = True
            owner._session_running = True
        owner._on_disconnect(old)
        await drain_callbacks(tasks)
        assert owner._client is replacement
        assert replacement.resources_open
        replacement.disconnect.assert_not_awaited()
        old.disconnect.assert_awaited_once()
        if kind == "brush":
            assert owner._direct_available and owner.data["live"]
        else:
            assert owner.data["bridge_connected"] and owner._session_running

    asyncio.run(exercise())


@pytest.mark.parametrize("kind", ["brush", "charger"])
def test_intentional_disconnect_callback_does_not_reconnect_or_double_close(kind):
    async def exercise():
        owner, tasks = make_owner(kind)
        client = BackendClient(True)
        owner._client = client
        original = client._disconnect

        async def disconnect():
            await original()
            owner._on_disconnect(client)
            await asyncio.sleep(0)

        client.disconnect.side_effect = disconnect
        await owner._async_disconnect()
        await drain_callbacks(tasks)
        client.disconnect.assert_awaited_once()
        assert owner._client is None
        if kind == "brush":
            owner._schedule_connect.assert_not_called()

    asyncio.run(exercise())


@pytest.mark.parametrize("kind", ["brush", "charger"])
def test_cancelled_disconnect_keeps_cleanup_alive_and_stop_waits(kind):
    async def exercise():
        owner, tasks = make_owner(kind)
        client = BackendClient()
        owner._client = client
        entered = asyncio.Event()
        release = asyncio.Event()

        async def disconnect():
            entered.set()
            await release.wait()
            await client._disconnect()

        client.disconnect.side_effect = disconnect
        closing = asyncio.create_task(owner._async_disconnect())
        await entered.wait()
        closing.cancel()
        with pytest.raises(asyncio.CancelledError):
            await closing
        assert client.resources_open
        assert tasks and not tasks[-1].done()
        stopping = asyncio.create_task(owner.async_stop())
        await asyncio.sleep(0)
        assert not stopping.done(), "unload returned while backend cleanup was pending"
        release.set()
        await stopping
        await drain_callbacks(tasks)
        assert not client.resources_open
        client.disconnect.assert_awaited_once()

    asyncio.run(exercise())


@pytest.mark.parametrize("kind", ["brush", "charger"])
def test_reconnect_waits_for_inflight_remote_cleanup(kind):
    async def exercise():
        owner, tasks = make_owner(kind)
        client = BackendClient()
        owner._client = client
        entered = asyncio.Event()
        release = asyncio.Event()
        module = coordinator if kind == "brush" else charger

        async def disconnect():
            entered.set()
            await release.wait()
            await client._disconnect()

        client.disconnect.side_effect = disconnect
        owner._on_disconnect(client)
        await entered.wait()

        async def establish(*args, **kwargs):
            assert not client.resources_open
            raise module.BleakError("simulated unavailable device")

        target = (
            patch.object(owner, "_async_establish_brush_connection", side_effect=establish)
            if kind == "brush"
            else patch.object(charger, "establish_connection", side_effect=establish)
        )
        with (
            patch.object(module.bluetooth, "async_ble_device_from_address", return_value=object()),
            target as connect,
        ):
            reconnect = asyncio.create_task(
                owner._async_connect() if kind == "brush"
                else owner._async_connect("11:22:33:44:55:66")
            )
            await asyncio.sleep(0)
            connect.assert_not_awaited()
            release.set()
            await reconnect
            connect.assert_awaited_once()
        await drain_callbacks(tasks)
        client.disconnect.assert_awaited_once()

    asyncio.run(exercise())


@pytest.mark.parametrize("kind", ["brush", "charger"])
@pytest.mark.parametrize(
    "error", [coordinator.BleakError("gone"), TimeoutError(), RuntimeError("backend failure")]
)
def test_cleanup_errors_do_not_break_disconnect_or_unload(kind, error):
    async def exercise():
        owner, tasks = make_owner(kind)
        client = BackendClient()
        client.disconnect.side_effect = error
        owner._client = client
        await owner._async_disconnect()
        await owner.async_stop()
        await drain_callbacks(tasks)
        assert owner._client is None
        client.disconnect.assert_awaited_once()

    asyncio.run(exercise())


@pytest.mark.parametrize("kind", ["brush", "charger"])
def test_remote_disconnect_during_stop_still_cleans_client(kind):
    async def exercise():
        owner, tasks = make_owner(kind)
        client = BackendClient()
        owner._client = client
        owner._stopping = True
        owner._on_disconnect(client)
        await drain_callbacks(tasks)
        await owner.async_stop()
        client.disconnect.assert_awaited_once()
        assert not client.resources_open
        if kind == "brush":
            owner._schedule_connect.assert_not_called()

    asyncio.run(exercise())


@pytest.mark.parametrize("kind", ["brush", "charger"])
def test_eager_home_assistant_task_creation_does_not_retain_finished_cleanup(kind):
    async def exercise():
        owner, tasks = make_owner(kind)

        def eager_create(coro):
            task = asyncio.Task(coro, loop=asyncio.get_running_loop(), eager_start=True)
            tasks.append(task)
            return task

        owner.hass.async_create_task.side_effect = eager_create
        client = BackendClient()
        owner._client = client
        await asyncio.wait_for(owner._async_disconnect(), timeout=1)
        assert not client.resources_open
        assert not owner._client_cleanup._tasks
        client.disconnect.assert_awaited_once()

    asyncio.run(exercise())


@pytest.mark.parametrize("kind", ["brush", "charger"])
@pytest.mark.parametrize(
    "stopping", [False, True], ids=["lost-before-handoff", "stop-during-connect"]
)
def test_connection_returned_after_loss_or_stop_is_cleaned(kind, stopping):
    async def exercise():
        owner, tasks = make_owner(kind)
        module = coordinator if kind == "brush" else charger
        client = BackendClient(stopping)
        client.start_notify = AsyncMock()

        async def establish(*args, **kwargs):
            if stopping:
                owner._stopping = True
            return client

        target = (
            patch.object(owner, "_async_establish_brush_connection", side_effect=establish)
            if kind == "brush"
            else patch.object(charger, "establish_connection", side_effect=establish)
        )
        with (
            patch.object(module.bluetooth, "async_ble_device_from_address", return_value=object()),
            target,
        ):
            if kind == "brush":
                await owner._async_connect()
            else:
                await owner._async_connect("11:22:33:44:55:66")
        await drain_callbacks(tasks)
        client.disconnect.assert_awaited_once()
        client.start_notify.assert_not_awaited()
        assert owner._client is None
        assert not client.resources_open

    asyncio.run(exercise())


@pytest.mark.parametrize("kind", ["brush", "charger"])
def test_cancelled_connection_setup_closes_owned_client(kind):
    async def exercise():
        owner, tasks = make_owner(kind)
        module = coordinator if kind == "brush" else charger
        client = BackendClient(True)
        entered = asyncio.Event()

        async def subscribe(*args):
            entered.set()
            await asyncio.Event().wait()

        client.start_notify = AsyncMock(side_effect=subscribe)
        target = (
            patch.object(owner, "_async_establish_brush_connection", return_value=client)
            if kind == "brush"
            else patch.object(charger, "establish_connection", return_value=client)
        )
        with (
            patch.object(module.bluetooth, "async_ble_device_from_address", return_value=object()),
            patch.object(owner, "_async_subscribe", side_effect=subscribe, create=True),
            target,
        ):
            connecting = asyncio.create_task(
                owner._async_connect() if kind == "brush"
                else owner._async_connect("11:22:33:44:55:66")
            )
            await entered.wait()
            connecting.cancel()
            with pytest.raises(asyncio.CancelledError):
                await connecting
        await drain_callbacks(tasks)
        client.disconnect.assert_awaited_once()
        assert not client.resources_open
        assert owner._client is None

    asyncio.run(exercise())


def test_explicit_charger_disconnect_ends_session_without_backend_callback():
    async def exercise():
        owner, tasks = make_owner("charger")
        owner.parent._charger_session_ended = MagicMock()
        owner._session_running = True
        owner._session_confirmed = True
        owner.parent.data["live"] = True
        owner._client = BackendClient()
        await owner._async_disconnect()
        await drain_callbacks(tasks)
        assert not owner._session_running
        assert not owner.parent.data["live"]
        owner.parent._charger_session_ended.assert_called_once_with()

    asyncio.run(exercise())


@pytest.mark.parametrize("kind", ["brush", "charger"])
def test_repeated_remote_disconnects_close_every_client(kind):
    async def exercise():
        owner, tasks = make_owner(kind)
        clients = []
        for _ in range(5):
            client = BackendClient()
            clients.append(client)
            owner._client = client
            # Some backends may deliver a duplicate disconnect callback.
            owner._on_disconnect(client)
            owner._on_disconnect(client)
            await drain_callbacks(tasks)
            assert all(not old.resources_open for old in clients)
        for client in clients:
            client.disconnect.assert_awaited_once()

    asyncio.run(exercise())


@pytest.mark.parametrize("kind", ["brush", "charger"])
def test_remote_callback_from_another_thread_runs_cleanup_on_home_assistant_loop(kind):
    async def exercise():
        owner, tasks = make_owner(kind)
        client = BackendClient()
        owner._client = client
        await asyncio.to_thread(owner._on_disconnect, client)
        await drain_callbacks(tasks)
        client.disconnect.assert_awaited_once()
        assert not client.resources_open

    asyncio.run(exercise())
