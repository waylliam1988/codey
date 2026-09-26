"""Shared plumbing for Ghost stores (no domain logic).

Ghost stays domain-split (affinity/continuity/router/work_queue keep their own
semantics). This module only owns the byte-identical helpers every store
hand-rolled: UTC timestamps and project scope normalization. Stores call these
directly; tests patch ``codey.ghost._common`` so there is exactly one seam.
"""

from __future__ import annotations

from collections.abc import Mapping
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


__all__ = ["VALID_SCOPES", "field_value", "normalize_project", "now_iso_z"]
