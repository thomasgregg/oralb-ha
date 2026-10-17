"""Stable charger transport behavior and logging privacy regressions."""

import asyncio
import importlib
import logging
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
charger = importlib.import_module("oralb_live.charger")


def bridge():
    parent = MagicMock()
    parent.data = {"live": False}
    result = charger.IOSenseBridge(parent)
    result._push = lambda: None
    result._client = MagicMock()
    result._client.is_connected = True
    return result


@pytest.mark.parametrize("level", [logging.DEBUG, logging.WARNING])
def test_logging_level_preserves_exact_transport_sequence(level, caplog):
    async def exercise():
        instance = bridge()

        async def write(uuid, payload, **kwargs):
            if payload == charger.IOSENSE_PROTOCOL_END:
                instance._on_read(None, bytearray.fromhex("37c108ff0101020012"))

        instance._client.write_gatt_char = AsyncMock(side_effect=write)
        with caplog.at_level(level, logger=charger.__name__):
            assert await instance._async_passthrough("FF08") == b"\x00\x12"
        calls = instance._client.write_gatt_char.call_args_list
        assert [c.args for c in calls] == [
            (
                charger.IOSENSE_COMMAND_UUID,
                bytes((0xC1, charger.ChargerCommand.BRUSH_PASSTHROUGH)),
            ),
            (charger.IOSENSE_WRITE_UUID, charger.build_passthrough_read("FF08")),
            (charger.IOSENSE_COMMAND_UUID, charger.IOSENSE_PROTOCOL_END),
        ]
        assert instance._pending_future is None

    asyncio.run(exercise())
    assert "iO Sense trace" not in caplog.text
    assert "payload='0012'" not in caplog.text


def test_negative_ack_remains_an_ack_not_a_brush_read(caplog):
    async def exercise():
        instance = bridge()
        instance._pending_command = charger.ChargerCommand.BRUSH_PASSTHROUGH
        instance._pending_short_uuid = "FF29"
        instance._pending_future = asyncio.get_running_loop().create_future()
        with caplog.at_level(logging.DEBUG, logger=charger.__name__):
            instance._on_status(None, bytearray.fromhex("37c1ff"))
        assert not instance._pending_future.done()
        instance._pending_future.cancel()

    asyncio.run(exercise())
    assert "iO Sense status: 37c1ff" in caplog.text
    assert "iO Sense trace" not in caplog.text


def test_timeout_logs_failure_and_cleans_pending_read(caplog):
    async def exercise():
        instance = bridge()
        instance._client.write_gatt_char = AsyncMock()
        with (
            caplog.at_level(logging.DEBUG, logger=charger.__name__),
            patch.object(charger, "CHARGER_BRIDGE_REQUEST_TIMEOUT_SECONDS", 0.01),
        ):
            assert await instance._async_passthrough("FF29") is None
        assert instance._pending_future is None
        assert instance._pending_short_uuid is None

    asyncio.run(exercise())
    assert "iO Sense passthrough FF29 failed" in caplog.text
    assert "iO Sense trace" not in caplog.text


def test_unsampled_fields_are_not_dumped(caplog):
    async def exercise():
        instance = bridge()

        async def write(uuid, payload, **kwargs):
            if payload == charger.IOSENSE_PROTOCOL_END:
                instance._on_read(None, bytearray.fromhex("37c105ff010103535553"))

        instance._client.write_gatt_char = AsyncMock(side_effect=write)
        with caplog.at_level(logging.DEBUG, logger=charger.__name__):
            assert await instance._async_passthrough("FF05") == b"SUS"

    asyncio.run(exercise())
    assert "payload=" not in caplog.text
    assert "535553" not in caplog.text
