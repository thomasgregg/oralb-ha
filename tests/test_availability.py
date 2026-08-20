"""Tests for toothbrush entity availability semantics."""

from __future__ import annotations

import importlib.util
import unittest
from pathlib import Path


AVAILABILITY_PATH = (
    Path(__file__).parents[1]
    / "custom_components"
    / "oralb_live"
    / "availability.py"
)
AVAILABILITY_SPEC = importlib.util.spec_from_file_location(
    "oralb_live_availability", AVAILABILITY_PATH
)
assert AVAILABILITY_SPEC and AVAILABILITY_SPEC.loader
availability = importlib.util.module_from_spec(AVAILABILITY_SPEC)
AVAILABILITY_SPEC.loader.exec_module(availability)


class ToothbrushEntityAvailabilityTests(unittest.TestCase):
    """Verify retained values remain readable for a sleepy toothbrush."""

    def test_fresh_source_is_available_and_not_assumed(self) -> None:
        self.assertEqual(
            availability.resolve_toothbrush_entity_availability(True, True),
            (True, False),
        )

    def test_fresh_source_can_expose_an_unknown_value(self) -> None:
        self.assertEqual(
            availability.resolve_toothbrush_entity_availability(True, False),
            (True, False),
        )

    def test_stale_source_keeps_a_retained_value_as_assumed(self) -> None:
        self.assertEqual(
            availability.resolve_toothbrush_entity_availability(False, True),
            (True, True),
        )

    def test_stale_source_without_a_value_is_unavailable(self) -> None:
        self.assertEqual(
            availability.resolve_toothbrush_entity_availability(False, False),
            (False, False),
        )


if __name__ == "__main__":
    unittest.main()
