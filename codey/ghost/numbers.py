"""Finite unit-interval float coercion shared by every Ghost store.

Ghost state (confidence/weight/priority/reward) must always land in [0, 1]
as a finite number. ``bool`` is never a valid numeric input here -- it would
silently coerce to 0.0/1.0 and poison learning state. NaN fails every
comparison, so range checks alone cannot reject it; :func:`math.isfinite`
is the load-bearing check.
"""

from __future__ import annotations

import math


def coerce_unit_float(value: object, *, digits: int = 4) -> float | None:
    """Strict coercion: unusable input projects to None (fail closed)."""
    if isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(number) or number < 0.0 or number > 1.0:
        return None
    return round(number, digits)


def clamp_unit_float(value: object, *, digits: int = 4) -> float:
    """Lenient coercion: unusable input becomes 0.0, out-of-range clamps."""
    if isinstance(value, bool):
        return 0.0
    try:
        number = float(value)
    except (TypeError, ValueError):
        return 0.0
    if not math.isfinite(number):
        return 0.0
    return round(max(0.0, min(1.0, number)), digits)


__all__ = [
    "clamp_unit_float",
    "coerce_unit_float",
]
