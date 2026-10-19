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
    warnings: object,
    *,
    limit: int,
    redact_sensitive: bool = False,
) -> tuple[str, ...]:
    """Clip, dedupe, and bound user-opaque warning strings."""
    if isinstance(warnings, str):
        warnings = (warnings,)
    if not isinstance(warnings, Iterable):
        return ()
    out: list[str] = []
    for warning in warnings:
        text = clip_signal_text(warning, WARNING_TEXT_LIMIT)
        if redact_sensitive and (not text or contains_sensitive_signal_text(text)):
            text = "redacted_warning"
        if text and text not in out:
            out.append(text)
        if len(out) >= max(1, limit):
            break
    return tuple(out)



__all__ = ["WARNING_TEXT_LIMIT", "bounded_warnings"]
