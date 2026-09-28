"""Neutral research synthesis helpers (no loop dependency)."""
from __future__ import annotations

from collections import Counter
from typing import Any


def synthesis_title(question: str, limit: int = 80) -> str:
    text = " ".join((question or "").split())
    if len(text) > limit:
        text = text[: limit - 1].rstrip() + "…"
    return text or "Research synthesis"


def run_concept_tags(store: Any, note_ids: list[str], limit: int = 5) -> list[str]:
    try:
        from codey.knowledge.concept_schema import normalize_concept
    except Exception:
        return []
    try:
        rows = store.index.tags_for([note_id for note_id in note_ids if note_id], active_only=True)
    except Exception:
        return []
    counts: Counter[str] = Counter()
    for row in rows or ():
        try:
            concept = normalize_concept(row.get("tag"))
        except Exception:
            concept = ""
        if concept:
            counts[concept] += 1
    return [concept for concept, _ in counts.most_common(limit)]


def synthesis_body(summary: str, ledger: Any) -> str:
    body = (summary or "").strip()
    try:
        from codey.research.runner import _ledger_appendix as _appendix  # lazy to avoid cycle
    except Exception:
        _appendix = None  # type: ignore[assignment]
    appendix = ""
    if callable(_appendix):
        try:
            appendix = str(_appendix(ledger) or "")
        except Exception:
            appendix = ""
    if not appendix:
        return body
    return f"{body}\n\n{appendix}".strip()


# Backward-compatible private names (old tests import these).
_run_concept_tags = run_concept_tags
_synthesis_title = synthesis_title
_synthesis_body = synthesis_body


__all__ = ["run_concept_tags", "synthesis_body", "synthesis_title"]
