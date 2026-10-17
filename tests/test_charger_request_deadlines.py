"""Regression tests for the complete charger request deadline.

Use short wire budgets and event-controlled transports; the outer watchdog only
keeps a regression from hanging pytest. No Home Assistant or hardware is used.
"""

from __future__ import annotations

import asyncio
import importlib
import sys
import types
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

_package = types.ModuleType("oralb_live")
_package.__path__ = [str(Path(__file__).parents[1] / "custom_components/oralb_live")]
sys.modules.setdefault("oralb_live", _package)
charger = importlib.import_module("oralb_live.charger")

BUDGET = 0.04
WATCHDOG = 0.5


@pytest.fixture
def bridge(monkeypatch):
    monkeypatch.setattr(charger, "CHARGER_BRIDGE_REQUEST_TIMEOUT_SECONDS", BUDGET)
    parent = MagicMock()
    parent.data = {"live": False}
    parent._async_apply_charger_passthrough = AsyncMock()
    instance = charger.IOSenseBridge(parent)
    instance._push = lambda: None
    instance._client = MagicMock()
    instance._client.is_connected = True
    return instance


def request(instance, kind):
    if kind == "get":
        return instance._async_get(charger.ChargerCommand.SESSION_STATUS)
    return instance._async_passthrough("FF08")


def reply(instance, kind):
    raw = "3ac000" if kind == "get" else "37c108ff0101020012"
    instance._on_read(None, bytearray.fromhex(raw))


def assert_clean(instance):
    assert instance._pending_future is None
    assert instance._pending_command is None
    assert instance._pending_short_uuid is None
    assert not instance._request_lock.locked()


async def cancel_tasks(*tasks):
    for task in tasks:
        if not task.done():
            task.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)


@pytest.mark.parametrize(
    "kind,stage",
    [
        ("get", 1),
        ("get", 2),
        ("passthrough", 1),
        ("passthrough", 2),
        ("passthrough", 3),
    ],
)
def test_deadline_covers_each_bluetooth_write(bridge, kind, stage):
    async def exercise():
        entered = asyncio.Event()
        never_finishes = asyncio.Event()
        calls = 0
        write_cancelled = False

        async def write(uuid, payload, **kwargs):
            nonlocal calls, write_cancelled
            calls += 1
            if calls == stage:
                entered.set()
                try:
                    await never_finishes.wait()
                finally:
                    write_cancelled = True
            if payload == charger.IOSENSE_PROTOCOL_END:
                reply(bridge, kind)

        bridge._client.write_gatt_char = AsyncMock(side_effect=write)
        task = asyncio.create_task(request(bridge, kind))
        try:
            await asyncio.wait_for(entered.wait(), WATCHDOG)
            done, _ = await asyncio.wait({task}, timeout=WATCHDOG)
            assert task in done, (
                f"{kind} write {stage} outlived the request deadline and retained the shared lock"
            )
            assert task.result() is None
            assert write_cancelled
            assert_clean(bridge)
        finally:
            await cancel_tasks(task)

    asyncio.run(exercise())


@pytest.mark.parametrize("kind", ["get", "passthrough"])
def test_writes_and_response_share_one_deadline(bridge, kind):
    async def exercise():
        handles = []

        async def write(uuid, payload, **kwargs):
            # All writes together fit, and the response fits on its own.
            # Their combined time must still use one deadline.
            write_count = 2 if kind == "get" else 3
            await asyncio.sleep(BUDGET * 0.6 / write_count)
            if payload == charger.IOSENSE_PROTOCOL_END:
                handles.append(
                    asyncio.get_running_loop().call_later(
                        BUDGET * 0.6, reply, bridge, kind
                    )
                )

        bridge._client.write_gatt_char = AsyncMock(side_effect=write)
        task = asyncio.create_task(request(bridge, kind))
        try:
            done, _ = await asyncio.wait({task}, timeout=WATCHDOG)
            assert task in done
            assert task.result() is None, (
                "Writes and response exceeded their shared budget"
            )
            assert_clean(bridge)
        finally:
            for handle in handles:
                handle.cancel()
            await cancel_tasks(task)

    asyncio.run(exercise())


def test_stalled_timer_does_not_block_post_session_battery_window(bridge):
    async def exercise():
        timer_entered = asyncio.Event()
        battery_applied = asyncio.Event()
        window_open = asyncio.Event()
        window_open.set()
        active_writes = 0
        max_active_writes = 0
        never_finishes = asyncio.Event()

        async def write(uuid, payload, **kwargs):
            nonlocal active_writes, max_active_writes
            active_writes += 1
            max_active_writes = max(max_active_writes, active_writes)
            try:
                if bridge._pending_short_uuid == "FF08":
                    timer_entered.set()
                    await never_finishes.wait()
                elif payload == charger.IOSENSE_PROTOCOL_END and window_open.is_set():
                    characteristic = bridge._pending_short_uuid
                    if characteristic == "FF05":
                        bridge._on_read(None, bytearray.fromhex("37c105ff0101015f"))
            finally:
                active_writes -= 1

        async def apply(characteristic, payload):
            if characteristic == "FF05":
                assert payload == b"\x5f"
                assert window_open.is_set()
                battery_applied.set()

        bridge._client.write_gatt_char = AsyncMock(side_effect=write)
        bridge.parent._async_apply_charger_passthrough = AsyncMock(side_effect=apply)
        timer = asyncio.create_task(bridge._async_passthrough("FF08"))
        recovery = observer = None
        close_window = None
        try:
            await asyncio.wait_for(timer_entered.wait(), WATCHDOG)
            close_window = asyncio.get_running_loop().call_later(
                BUDGET * 4, window_open.clear
            )
            recovery = asyncio.create_task(bridge._async_post_session_sync(force=True))
            observer = asyncio.create_task(battery_applied.wait())
            done, _ = await asyncio.wait({observer}, timeout=WATCHDOG)
            assert observer in done, (
                "Post-session FF05 could not acquire the request lock before the brush window closed"
            )
            assert timer.done() and timer.result() is None
            assert max_active_writes == 1, (
                "Recovery must not overlap an abandoned timer write"
            )
        finally:
            if close_window:
                close_window.cancel()
            await cancel_tasks(
                *(t for t in (timer, recovery, observer) if t is not None)
            )
        assert_clean(bridge)

    asyncio.run(exercise())


@pytest.mark.parametrize("kind", ["get", "passthrough"])
def test_successful_early_reply_preserves_exact_write_order(bridge, kind):
    async def exercise():
        async def write(uuid, payload, **kwargs):
            if payload == charger.IOSENSE_PROTOCOL_END:
                # A notification may arrive before write_gatt_char returns.
                reply(bridge, kind)

        bridge._client.write_gatt_char = AsyncMock(side_effect=write)
        result = await request(bridge, kind)
        assert result is not None
        actual = [call.args for call in bridge._client.write_gatt_char.call_args_list]
        if kind == "get":
            expected = [
                (
                    charger.IOSENSE_COMMAND_UUID,
                    charger.build_charger_get(charger.ChargerCommand.SESSION_STATUS),
                )
            ]
        else:
            expected = [
                (
                    charger.IOSENSE_COMMAND_UUID,
                    bytes((0xC1, charger.ChargerCommand.BRUSH_PASSTHROUGH)),
                ),
                (charger.IOSENSE_WRITE_UUID, charger.build_passthrough_read("FF08")),
            ]
        expected.append((charger.IOSENSE_COMMAND_UUID, charger.IOSENSE_PROTOCOL_END))
        assert actual == expected
        assert_clean(bridge)

    asyncio.run(exercise())


@pytest.mark.parametrize("kind", ["get", "passthrough"])
@pytest.mark.parametrize("phase", ["write", "response"])
def test_external_cancellation_propagates_and_releases_lock(bridge, kind, phase):
    async def exercise():
        reached = asyncio.Event()
        never_finishes = asyncio.Event()

        async def write(uuid, payload, **kwargs):
            if phase == "write":
                reached.set()
                await never_finishes.wait()
            elif payload == charger.IOSENSE_PROTOCOL_END:
                reached.set()

        bridge._client.write_gatt_char = AsyncMock(side_effect=write)
        task = asyncio.create_task(request(bridge, kind))
        try:
            await asyncio.wait_for(reached.wait(), WATCHDOG)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            assert_clean(bridge)
        finally:
            await cancel_tasks(task)

    asyncio.run(exercise())


@pytest.mark.parametrize("kind", ["get", "passthrough"])
def test_response_timeout_remains_a_failed_clean_request(bridge, kind):
    async def exercise():
        bridge._client.write_gatt_char = AsyncMock()
        assert await request(bridge, kind) is None
        assert_clean(bridge)

    asyncio.run(exercise())


@pytest.mark.parametrize("kind", ["get", "passthrough"])
def test_queue_wait_does_not_consume_the_next_wire_budget(bridge, kind):
    async def exercise():
        await bridge._request_lock.acquire()

        async def write(uuid, payload, **kwargs):
            if payload == charger.IOSENSE_PROTOCOL_END:
                reply(bridge, kind)

        bridge._client.write_gatt_char = AsyncMock(side_effect=write)
        task = asyncio.create_task(request(bridge, kind))
        unlock = asyncio.get_running_loop().call_later(
            BUDGET * 2, bridge._request_lock.release
        )
        try:
            assert await asyncio.wait_for(task, WATCHDOG) is not None
            assert_clean(bridge)
        finally:
            unlock.cancel()
            await cancel_tasks(task)

    asyncio.run(exercise())


def test_late_timer_response_cannot_complete_a_battery_request(bridge):
    async def exercise():
        bridge._client.write_gatt_char = AsyncMock()
        assert await bridge._async_passthrough("FF08") is None
        old_reply_seen = False

        async def write(uuid, payload, **kwargs):
            nonlocal old_reply_seen
            if payload == charger.IOSENSE_PROTOCOL_END:
                bridge._on_read(None, bytearray.fromhex("37c108ff0101020012"))
                assert not bridge._pending_future.done()
                old_reply_seen = True
                bridge._on_read(None, bytearray.fromhex("37c105ff0101015f"))

        bridge._client.write_gatt_char = AsyncMock(side_effect=write)
        assert await bridge._async_passthrough("FF05") == b"\x5f"
        assert old_reply_seen
        assert_clean(bridge)

    asyncio.run(exercise())
