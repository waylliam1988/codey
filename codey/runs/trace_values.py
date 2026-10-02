"""Trace-only pure sanitation helpers shared by every projection.

Byte-identical moves from the former ``trace.py`` tail. These helpers
never touch recorder state, files, or manifests: they map untrusted
inputs to bounded values. Deliberately NOT unified with the
similarly-named helpers in ``utils/refs.py`` — the ``bool``/edge
contracts differ and each layer owns its own.
"""

from __future__ import annotations

import math
from collections.abc import Iterable, Mapping

from codey.policies.redaction import looks_sensitive_code
from codey.runs.text_clip import clip_text as _clip
from codey.runs.trace_schema import MAX_REFS, MAX_TEXT_CHARS, MAX_WARNINGS
from codey.utils.refs import coerce_float, coerce_int, parse_int


def _safe_trace_code(value: object, limit: int) -> str:
    raw = _clip(value, limit)
    if not raw:
        return ""
    if looks_sensitive_code(raw):
        return ""
    text = _identifier(raw, limit)
    if looks_sensitive_code(text):
        return ""
    return text


def _projection_codes(projection: Mapping[str, object], key: str) -> list[str]:
    return [
        code
        for code in (
            _safe_trace_code(value, 80)
            for value in _trace_list_items(projection.get(key))
        )
        if code
    ][:MAX_WARNINGS]


def _trace_list_items(value: object) -> tuple[object, ...]:
    if isinstance(value, (list, tuple)):
        return tuple(value)
    if isinstance(value, (set, frozenset)):
        return tuple(sorted(value, key=str))
    return ()


def _nonnegative_int(value: object) -> int:
    if isinstance(value, bool):
        return 0
    try:
        return max(0, coerce_int(value))
    except (TypeError, ValueError, OverflowError):
        return 0


def _bounded_int(value: object, lower: int, upper: int) -> int:
    if isinstance(value, bool):
        return lower
    try:
        parsed = coerce_int(value, default=lower)
    except (TypeError, ValueError, OverflowError):
        parsed = lower
    return max(lower, min(upper, parsed))


def _unit_float(value: object) -> float:
    if isinstance(value, bool):
        return 0.0
    try:
        number = coerce_float(value)
    except (TypeError, ValueError, OverflowError):
        return 0.0
    if not math.isfinite(number):
        return 0.0
    if number < 0:
        return 0.0
    if number > 1:
        return 1.0
    return round(number, 3)


def _int_or_none(value: object) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, str) and not value.strip().isascii():
        return None
    try:
        parsed = parse_int(value)
        return None if parsed is None else max(0, parsed)
    except (TypeError, ValueError, OverflowError):
        return None


def _identifier(value: object, limit: int = MAX_TEXT_CHARS) -> str:
    text = _clip(value, limit)
    return "".join(char if char.isalnum() or char in "._:-" else "_" for char in text)


def _tool_instance_id(value: object) -> str:
    text = _identifier(value, 40)
    turn, sep, index = text.partition(":")
    if not sep:
        return ""
    if not turn.isascii() or not turn.isdigit():
        return ""
    if not index.isascii() or not index.isdigit():
        return ""
    return text


def _bounded_refs(values: Iterable[object], *, limit: int = MAX_REFS) -> tuple[str, ...]:
    if isinstance(values, str):
        values = (values,)
    refs: list[str] = []
    seen: set[str] = set()
    for value in values or ():
        text = _clip(value, 160)
        if not text or text in seen:
            continue
        seen.add(text)
        refs.append(text)
        if len(refs) >= limit:
            break
    return tuple(refs)
