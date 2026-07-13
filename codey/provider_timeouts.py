"""Shared deadline helpers for bounded provider recovery operations."""

from __future__ import annotations

import math
import time


def start_deadline(timeout: float | None) -> float | None:
    if timeout is None:
        return None
    return time.monotonic() + max(0.0, float(timeout))


def remaining(deadline: float | None, default: float) -> float:
    if deadline is None:
        return float(default)
    value = deadline - time.monotonic()
    if value <= 0:
        raise TimeoutError("Provider recovery time budget was exhausted")
    return value


def navigation_timeout_ms(
    deadline: float | None,
    default_ms: int = 60_000,
) -> int:
    if deadline is None:
        return default_ms
    return max(1, min(default_ms, math.ceil(remaining(deadline, 0.0) * 1000)))
