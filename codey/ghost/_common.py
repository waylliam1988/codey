"""Shared plumbing for Ghost stores (no domain logic).

Ghost stays domain-split (affinity/continuity/router/work_queue keep their own
semantics). This module only owns the byte-identical helpers every store
hand-rolled: UTC timestamps, project scope normalization, and strict
payload/validation helpers. Stores call these directly; tests patch
``codey.ghost._common`` so there is exactly one seam.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from codey.ghost.schema import clip_signal_text

VALID_SCOPES = ("user", "project", "session")


def field_value(value: Any, name: str) -> object:
    """Read a dict-or-object field without inventing a default type."""
    if isinstance(value, Mapping):
        return value.get(name)
    return getattr(value, name, "")


def now_iso_z() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def normalize_project(value: object) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    try:
        return clip_signal_text(Path(text).expanduser().resolve(), 240)
    except (OSError, RuntimeError, ValueError):
        return clip_signal_text(text, 240)


def strict_payload_equal(value: object, expected: object) -> bool:
    if isinstance(expected, Mapping):
        if not isinstance(value, Mapping) or set(value.keys()) != set(expected.keys()):
            return False
        return all(strict_payload_equal(value[key], expected[key]) for key in expected)
    if isinstance(expected, list):
        if not isinstance(value, list) or len(value) != len(expected):
            return False
        return all(
            strict_payload_equal(item, expected_item) for item, expected_item in zip(value, expected, strict=True)
        )
    return type(value) is type(expected) and value == expected


def mapping_keys_within(value: Mapping[str, object], allowed: Iterable[str]) -> bool:
    allowed_keys = set(allowed)
    return all(isinstance(key, str) and key in allowed_keys for key in value)


def filter_values(value: object, allowed: frozenset[str]) -> set[str]:
    values = {str(item).strip().lower() for item in str(value or "").split(",") if str(item).strip()}
    return {item for item in values if item in allowed}


__all__ = [
    "VALID_SCOPES",
    "field_value",
    "filter_values",
    "mapping_keys_within",
    "normalize_project",
    "now_iso_z",
    "strict_payload_equal",
]
