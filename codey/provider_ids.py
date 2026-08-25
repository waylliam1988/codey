"""Canonical provider identity rules shared by config parsing and runtime.

Provider ids arrive from user config, project config, the registry, and the
supervisor. One normalization rule keeps them comparable everywhere:

- lowercase, trimmed;
- kept only if alphanumeric apart from ``-`` and ``_``;
- anything else normalizes to the empty string, which callers must treat as
  "no provider" instead of guessing.
"""

from __future__ import annotations

from typing import Iterable


def normalize_provider_id(value: object) -> str:
    text = str(value or "").strip().lower()
    if not text:
        return ""
    return text if text.replace("-", "").replace("_", "").isalnum() else ""


def normalize_provider_ids(values: Iterable[object]) -> tuple[str, ...]:
    """Normalize a sequence, dropping empties and duplicates in order."""

    ordered: list[str] = []
    seen: set[str] = set()
    for value in values:
        provider_id = normalize_provider_id(value)
        if not provider_id or provider_id in seen:
            continue
        seen.add(provider_id)
        ordered.append(provider_id)
    return tuple(ordered)


__all__ = [
    "normalize_provider_id",
    "normalize_provider_ids",
]
