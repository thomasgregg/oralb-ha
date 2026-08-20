"""Availability rules for sleepy Oral-B toothbrush entities."""

from __future__ import annotations


def resolve_toothbrush_entity_availability(
    source_available: bool, has_value: bool
) -> tuple[bool, bool]:
    """Return entity availability and whether its retained value is assumed."""
    return source_available or has_value, not source_available and has_value
