"""Leaf guard helpers for bounded Research payloads."""

from __future__ import annotations

from collections.abc import Iterable

from codey.utils.refs import clip, identifier


def bounded_int(
    value: object,
    lower: int,
    upper: int,
    *,
    default: int | None = None,
) -> int:
    fallback = lower if default is None else default
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        parsed = int(fallback)
    return max(lower, min(upper, parsed))


def status_token(
    value: object,
    allowed: Iterable[str],
    *,
    default: str,
    limit: int = 40,
) -> str:
    text = identifier(value, limit).lower()
    allowed_set = set(allowed)
    return text if text in allowed_set else default


def identifier_schema_ok(value: object, limit: int, *, allow_empty: bool = True) -> bool:
    if not isinstance(value, str):
        return False
    if not value:
        return allow_empty
    return value == identifier(value, limit)


def clip_schema_ok(value: object, limit: int, *, allow_empty: bool = True) -> bool:
    if not isinstance(value, str):
        return False
    if not value:
        return allow_empty
    return value == clip(value, limit)


__all__ = [
    "bounded_int",
    "clip",
    "clip_schema_ok",
    "identifier",
    "identifier_schema_ok",
    "status_token",
]
