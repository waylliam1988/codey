"""Lossless task text units, derived from the original task rather than a model plan."""

from __future__ import annotations

import re
from typing import Any

_BOUNDARY = re.compile(r"[\n,;!?\u3002\uff0c\uff1b\uff01\uff1f]+|\.(?=\s|$)")
_LEGACY_SENTENCE_BOUNDARY = re.compile(r"[\n!?\u3002\uff01\uff1f]+|\.(?=\s|$)")
_CLOSERS = r'''["'’”»）)\]】》」』}]*'''
_SENTENCE_BOUNDARY = re.compile(r"(?:[\n!?\u3002\uff01\uff1f]+|\.(?=" + _CLOSERS + r"(?:\s|$)))" + _CLOSERS)


def text_units(text: str, *, sentences: bool = False, legacy_sentence_boundaries: bool = False) -> list[dict[str, Any]]:
    """Retain original offsets, punctuation and whitespace without semantic inference."""
    rows: list[dict[str, Any]] = []
    start = 0
    boundary = _SENTENCE_BOUNDARY if sentences else _BOUNDARY
    if sentences and legacy_sentence_boundaries:
        boundary = _LEGACY_SENTENCE_BOUNDARY
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


def validate_task_accounting(coverage: list[dict[str, Any]], task: str) -> None:
    """Check explicit accounting, never whether an answer means what it claims."""
    expected = {row["task_ref"] for row in task_requirements(task)}
    observed: set[str] = set()
    for index, row in enumerate(coverage):
        refs = row.get("task_refs")
        if (not isinstance(refs, list) or any(not isinstance(ref, str) for ref in refs)
                or len(refs) != len(set(refs)) or not set(refs) <= expected):
            raise ValueError(f"report_sections.coverage[{index}].task_refs must select original task rN ids: "
                             + ", ".join(sorted(expected)))
        observed.update(refs)
    if observed != expected:
        raise ValueError("report_sections.coverage must account for every original task unit in task_refs; missing: "
                         + ", ".join(sorted(expected - observed))
                         + ". Give its answer or specific unresolved gap; do not drop the obligation.")
