"""Regression tests for coordinator task scheduling."""

from __future__ import annotations

import ast
import unittest
from pathlib import Path


COORDINATOR_PATH = (
    Path(__file__).parents[1]
    / "custom_components"
    / "oralb_live"
    / "coordinator.py"
)


class CoordinatorTaskSchedulingTests(unittest.TestCase):
    """Verify lifetime coordinator loops do not block Home Assistant startup."""

    def test_reconnect_loop_is_a_named_background_task(self) -> None:
        """The direct-mode reconnect backstop must not be a setup task."""
        module = ast.parse(COORDINATOR_PATH.read_text(encoding="utf-8"))
        coordinator = next(
            node
            for node in module.body
            if isinstance(node, ast.ClassDef) and node.name == "OralBLiveCoordinator"
        )
        async_start = next(
            node
            for node in coordinator.body
            if isinstance(node, ast.FunctionDef) and node.name == "async_start"
        )
        reconnect_assignment = next(
            node
            for node in ast.walk(async_start)
            if isinstance(node, ast.Assign)
            and any(
                isinstance(target, ast.Attribute)
                and target.attr == "_reconnect_task"
                for target in node.targets
            )
        )

        self.assertIsInstance(reconnect_assignment.value, ast.Call)
        call = reconnect_assignment.value
        assert isinstance(call, ast.Call)
        self.assertIsInstance(call.func, ast.Attribute)
        assert isinstance(call.func, ast.Attribute)
        self.assertEqual(call.func.attr, "async_create_background_task")
        self.assertGreaterEqual(len(call.args), 2)
        self.assertIsInstance(call.args[1], ast.Constant)
        self.assertEqual(call.args[1].value, "oralb_live_reconnect_loop")

    def test_sync_sequence_is_a_named_background_task(self) -> None:
        """A settling or retrying session sync must not delay startup."""
        module = ast.parse(COORDINATOR_PATH.read_text(encoding="utf-8"))
        coordinator = next(
            node
            for node in module.body
            if isinstance(node, ast.ClassDef) and node.name == "OralBLiveCoordinator"
        )
        maybe_schedule_sync = next(
            node
            for node in coordinator.body
            if isinstance(node, ast.FunctionDef)
            and node.name == "_maybe_schedule_sync"
        )
        sync_assignment = next(
            node
            for node in ast.walk(maybe_schedule_sync)
            if isinstance(node, ast.Assign)
            and any(
                isinstance(target, ast.Attribute) and target.attr == "_sync_task"
                for target in node.targets
            )
        )

        self.assertIsInstance(sync_assignment.value, ast.Call)
        call = sync_assignment.value
        assert isinstance(call, ast.Call)
        self.assertIsInstance(call.func, ast.Attribute)
        assert isinstance(call.func, ast.Attribute)
        self.assertEqual(call.func.attr, "async_create_background_task")
        self.assertGreaterEqual(len(call.args), 2)
        self.assertIsInstance(call.args[1], ast.Constant)
        self.assertEqual(call.args[1].value, "oralb_live_sync_sequence")


if __name__ == "__main__":
    unittest.main()
