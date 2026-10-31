"""Lossless task text units, derived from the original task rather than a model plan."""

from __future__ import annotations

import re
from typing import Any

_BOUNDARY = re.compile(r"[\n,;!?\u3002\uff0c\uff1b\uff01\uff1f]+|\.(?=\s|$)")
_SENTENCE_BOUNDARY = re.compile(r"[\n!?\u3002\uff01\uff1f]+|\.(?=\s|$)")


def text_units(text: str, *, sentences: bool = False) -> list[dict[str, Any]]:
    """Retain original offsets, punctuation and whitespace without semantic inference."""
    rows: list[dict[str, Any]] = []
    start = 0
    boundary = _SENTENCE_BOUNDARY if sentences else _BOUNDARY
    for end in [*(match.end() for match in boundary.finditer(text)), len(text)]:
        span = text[start:end]
        if span.strip():
            rows.append({"unit": len(rows) + 1, "start": start, "end": end, "text": span})
        start = end
    if not rows:
        raise ValueError("research scope requires nonempty text")
    return rows


def task_requirements(task: str) -> list[dict[str, Any]]:
    """Freeze every nonblank span; text segmentation is not semantic interpretation."""
    return [{"task_ref": f"r{row['unit']}", **{key: row[key] for key in ("start", "end", "text")}}
            for row in text_units(task)]
