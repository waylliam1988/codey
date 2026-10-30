"""Bounded read-only projection of research receipts and pinned knowledge."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from codey.knowledge.model import canonical_json


@dataclass(frozen=True)
class ResearchContextView:
    questions: tuple[dict[str, Any], ...]
    references: tuple[dict[str, Any], ...]
    investigations: tuple[dict[str, Any], ...]
    recent_results: tuple[dict[str, Any], ...]
    saved_evidence_count: int
    omitted_references: int = 0

    def render(self) -> str:
        return canonical_json({"questions": self.questions, "references": self.references,
            "investigations": self.investigations, "recent_results": self.recent_results,
            "saved_evidence_count": self.saved_evidence_count,
            "omitted_references": self.omitted_references})


def _bounded(row: dict[str, Any], budget: int) -> dict[str, Any]:
    # A truncated condition is never silently presented as complete.
    if len(canonical_json(row)) <= budget:
        return row
    preview = {key: value for key, value in row.items()
               if key in {"ref", "kind", "status", "interpretation_review", "dependencies_changed"}}
    preview.update(display_truncated=True, detail_available=True)
    for key in ("text", "conditions", "limitations", "excerpt"):
        if key not in row:
            continue
        value = row[key] if isinstance(row[key], str) else canonical_json(row[key])
        room = budget - len(canonical_json(preview)) - len(key) - 16
        if room > 20:
            preview[key] = value[:room]
    return preview


def build_research_context_view(session: Any, ledger: Any = None, store: Any = None, *,
                                budget: int = 14000) -> ResearchContextView:
    if budget < 4000:
        raise ValueError("research context budget must be at least 4000 characters")
    questions = tuple({"question_ref": f"q{index}", "question": text}
        for index, text in enumerate(ledger.questions if ledger is not None else session.research_questions, 1))
    recent = tuple({"tool": result.call.name, "ok": result.ok,
        **({"error": result.model_text[:400]} if not result.ok else {})}
        for result in list(session._memory_results.values())[-4:])
    attempts = tuple({"ref": label, "tool": row["tool"], "ok": row["ok"],
        "target": str(next((row.get("args", {}).get(key) for key in
            ("query", "url", "source_ref", "result_ref") if row.get("args", {}).get(key)), ""))[:180]}
        for label, row in (list(ledger.investigations.items())[-8:] if ledger is not None else []))
    rows = []
    if ledger is not None:
        labels = sorted(set(ledger.finding_refs) | set(ledger.knowledge_refs), key=lambda key: int(key[1:]))
        for label in labels:
            if store is not None:
                from codey.research.references import resolve_version

                version = resolve_version(ledger, store, label)
                rows.append({"ref": label, "kind": version.value.kind, "text": version.value.content,
                    "status": version.value.status, "interpretation_review": version.interpretation_review,
                    "dependencies_changed": version.dependencies_changed, "conditions": version.value.conditions,
                    "limitations": version.value.provenance.get("limitations", ""), "detail_available": True})
            else:
                items = [item for item in ledger.evidence_items if item.note_id == ledger.finding_refs.get(label)]
                rows.append({"ref": label, "text": items[0].claim if items else "Read details before adopting.",
                    "kind": "claim", "interpretation_review": "unreviewed", "detail_available": True})
    # No guessed question/finding associations. Reserve space for all questions,
    # then share the remainder across references, preserving early discoveries.
    base = ResearchContextView(questions, (), attempts, recent,
        len(ledger.evidence_items) if ledger is not None else len(session.evidence))
    room = max(0, budget - len(base.render()) - 100)
    count = min(len(rows), room // 260)
    per_ref = max(260, room // count - 2) if count else 260
    return ResearchContextView(questions, tuple(_bounded(row, per_ref) for row in rows[:count]),
        attempts, recent, base.saved_evidence_count, len(rows) - count)
