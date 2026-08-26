"""Regression tests for provisional iO Sense charger sessions."""

from __future__ import annotations

import importlib
import pathlib
import sys
import types
import unittest
from unittest.mock import MagicMock

COMPONENT_PATH = pathlib.Path(__file__).parents[1] / "custom_components" / "oralb_live"

_PACKAGE = types.ModuleType("oralb_live")
_PACKAGE.__path__ = [str(COMPONENT_PATH)]
sys.modules.setdefault("oralb_live", _PACKAGE)

charger = importlib.import_module("oralb_live.charger")
charger_protocol = importlib.import_module("oralb_live.charger_protocol")


class ChargerProvisionalSessionTests(unittest.TestCase):
    """pre_run may poll the brush but must not create user-visible activity."""

    def setUp(self) -> None:
        charger.async_dispatcher_send = lambda *args, **kwargs: None
        self.parent = MagicMock()
        self.parent.address = "AA:BB:CC:DD:EE:FF"
        self.parent.data = {"live": False}
        self.parent._session_active = False
        self.bridge = charger.IOSenseBridge(self.parent)
        self.bridge._ensure_live_task = lambda: None
        self.bridge._schedule_disconnect = lambda: None
        self.bridge._cancel_disconnect = lambda: None

    def _packet(self, command, value):
        return charger_protocol.ChargerPacket(
            command=command,
            command_id=int(command),
            operation=0,
            value=value,
            payload=b"",
            raw=b"",
        )

    def _native(self, command, value) -> None:
        self.bridge._apply_native_packet(self._packet(command, value))

    def test_pre_run_then_quiet_never_starts_parent_session(self) -> None:
        self._native(charger_protocol.ChargerCommand.SESSION_STATUS, "inactive")
        self._native(charger_protocol.ChargerCommand.BRUSH_STATUS, "pre_run")

        self.assertTrue(self.bridge._session_running)
        self.assertFalse(self.bridge._session_confirmed)
        self.parent._charger_session_started.assert_not_called()

        self._native(charger_protocol.ChargerCommand.BRUSH_STATUS, "charging")

        self.assertFalse(self.bridge._session_running)
        self.parent._charger_session_started.assert_not_called()
        self.parent._charger_session_ended.assert_not_called()

    def test_native_run_promotes_provisional_session(self) -> None:
        self._native(charger_protocol.ChargerCommand.BRUSH_STATUS, "pre_run")
        self._native(charger_protocol.ChargerCommand.BRUSH_STATUS, "run")

        self.assertTrue(self.bridge._session_confirmed)
        self.parent._charger_session_started.assert_called_once_with(confirmed=True)

        self._native(charger_protocol.ChargerCommand.BRUSH_STATUS, "idle")
        self.parent._charger_session_ended.assert_called_once_with()

    def test_ff04_running_promotes_provisional_session(self) -> None:
        self._native(charger_protocol.ChargerCommand.BRUSH_STATUS, "pre_run")

        self.assertTrue(
            self.bridge._observe_provisional_passthrough("FF04", b"\x03\x00")
        )
        self.parent._charger_session_started.assert_called_once_with(confirmed=True)

    def test_advancing_ff08_promotes_but_retained_timer_does_not(self) -> None:
        self._native(charger_protocol.ChargerCommand.BRUSH_STATUS, "pre_run")

        self.assertFalse(
            self.bridge._observe_provisional_passthrough("FF08", b"\x00\x12")
        )
        self.assertFalse(
            self.bridge._observe_provisional_passthrough("FF08", b"\x00\x12")
        )
        self.parent._charger_session_started.assert_not_called()

        self.assertTrue(
            self.bridge._observe_provisional_passthrough("FF08", b"\x00\x13")
        )
        self.parent._charger_session_started.assert_called_once_with(confirmed=True)

    def test_unavailable_discards_provisional_session(self) -> None:
        self._native(charger_protocol.ChargerCommand.BRUSH_STATUS, "pre_run")
        self.bridge._async_unavailable(MagicMock())

        self.assertFalse(self.bridge._session_running)
        self.parent._charger_session_started.assert_not_called()
        self.parent._charger_session_ended.assert_not_called()

    def test_transport_disconnect_ends_confirmed_session(self) -> None:
        self._native(charger_protocol.ChargerCommand.BRUSH_STATUS, "run")
        self.bridge._handle_transport_disconnect()

        self.assertFalse(self.bridge._session_running)
        self.parent._charger_session_ended.assert_called_once_with()
        self.assertFalse(self.parent.data["live"])
