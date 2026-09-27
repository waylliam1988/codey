"""Shared positive-int coercion (single source for identical helpers)."""

from __future__ import annotations


def positive_int(value: object) -> int | None:
    """Return a positive int or None.

    Bool is never a valid count (``True`` must not pass as ``1``).
    """
    if isinstance(value, bool):
        return None
    try:
        number = int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError, OverflowError):
        return None
    return number if number > 0 else None


__all__ = ["positive_int"]
