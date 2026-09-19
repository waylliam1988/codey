"""Shared warning helpers for Ghost stores.

Behavior-equivalent extraction: each store previously hand-rolled the same
clip/dedupe/limit loop with a different event-file prefix. This module owns
the loop once; stores only pass their stream name, limit, and whether
sensitive text must be redacted. No ranking or eviction logic lives here.
"""

from __future__ import annotations

from collections.abc import Iterable

from codey.ghost.schema import clip_signal_text, contains_sensitive_signal_text

WARNING_TEXT_LIMIT = 180


def bounded_warnings(
    warnings: Iterable[object],
    *,
    limit: int,
    redact_sensitive: bool = False,
) -> tuple[str, ...]:
    """Clip, dedupe, and bound user-opaque warning strings."""
    out: list[str] = []
    for warning in warnings:
        text = clip_signal_text(warning, WARNING_TEXT_LIMIT)
        if redact_sensitive and (not text or contains_sensitive_signal_text(text)):
            text = "redacted_warning"
        if text and text not in out:
            out.append(text)
        if len(out) >= max(1, int(limit)):
            break
    return tuple(out)


def map_event_warnings(warnings: Iterable[str], *, stream: str) -> list[str]:
    """Map raw event-file warnings to store-scoped warning ids."""
    mapped: list[str] = []
    too_large = f"{stream}.jsonl:too_large"
    unreadable = f"{stream}.jsonl:unreadable"
    for warning in warnings:
        if warning == too_large:
            mapped.append(f"{stream}_too_large")
        elif warning == unreadable:
            mapped.append(f"{stream}_unreadable")
        else:
            mapped.append(str(warning))
    return mapped


def event_read_warnings(
    warnings: Iterable[str],
    *,
    stream: str,
    limit: int,
    redact_sensitive: bool = False,
) -> tuple[str, ...]:
    """Map event-file warnings then apply the shared bounded loop."""
    return bounded_warnings(
        map_event_warnings(warnings, stream=stream),
        limit=limit,
        redact_sensitive=redact_sensitive,
    )


def slice_event_warnings(
    warnings: Iterable[str],
    *,
    stream: str,
    limit: int,
) -> tuple[str, ...]:
    """Map event-file warnings without clipping (legacy slice stores)."""
    return tuple(map_event_warnings(warnings, stream=stream)[:max(1, int(limit))])


__all__ = [
    "WARNING_TEXT_LIMIT",
    "bounded_warnings",
    "event_read_warnings",
    "map_event_warnings",
    "slice_event_warnings",
]
