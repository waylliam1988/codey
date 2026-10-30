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
    budget_issue: str = ""

    def render(self) -> str:
        return canonical_json({"questions": self.questions, "references": self.references,
            "investigations": self.investigations, "recent_results": self.recent_results,
            "saved_evidence_count": self.saved_evidence_count,
            "omitted_references": self.omitted_references, "budget_issue": self.budget_issue})


def _bounded(row: dict[str, Any], budget: int) -> dict[str, Any]:
    # A truncated condition is never silently presented as complete.
    if len(canonical_json(row)) <= budget:
        return row
    preview = {key: value for key, value in row.items()
               if key in {"ref", "kind", "status", "interpretation_review", "dependencies_changed", "evidence_basis"}}
    preview.update(display_truncated=True, detail_available=True)
    # Give original evidence room before filling the remainder with model text.
    for key in ("excerpt", "text", "conditions", "limitations"):
        if key not in row:
            continue
        value = row[key] if isinstance(row[key], str) else canonical_json(row[key])
        lower, upper = 0, min(len(value), 500 if key in {"excerpt", "text"} else len(value))
        while lower < upper:
            middle = (lower + upper + 1) // 2
            candidate = {**preview, key: value[:middle]}
            if len(canonical_json(candidate)) <= budget:
                lower = middle
            else:
                upper = middle - 1
        if lower:
            preview[key] = value[:lower]
    return preview


def build_research_context_view(session: Any, ledger: Any = None, store: Any = None, *,
                                budget: int = 14000, session_id: str = "") -> ResearchContextView:
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
        labels = sorted(ledger.references, key=lambda key: int(key[1:]))
        for label in labels:
            if store is not None:
                from codey.research.references import resolve_version

                version = resolve_version(ledger, store, label)
                rows.append({"ref": label, "kind": version.value.kind, "text": version.value.content,
                    "status": version.value.status, "interpretation_review": version.interpretation_review,
                    "dependencies_changed": version.dependencies_changed, "conditions": version.value.conditions,
                    "limitations": version.value.provenance.get("limitations", ""), "detail_available": True})
                from codey.research.evidence_view import reference_evidence_preview

                preview = reference_evidence_preview(ledger, store, label,
                    project=str(getattr(session, "project", "")), session_id=session_id)
                rows[-1].update(excerpt="\n".join(row["excerpt"] for row in preview["evidence"]),
                    evidence_basis=preview.get("evidence_basis", "unavailable"),
                    evidence_complete=preview["evidence_complete"])
            else:
                items = [item for item in ledger.evidence_items if item.note_id == ledger.references[label].note_id]
                rows.append({"ref": label, "text": items[0].claim if items else "Read details before adopting.",
                    "kind": ledger.references[label].kind, "interpretation_review": "unreviewed",
                    "excerpt": items[0].excerpt if items else "", "detail_available": True})
    # Questions keep their full conditions. Drop optional receipts first; if
    # even the questions exceed the hard cap, emit an explicit incomplete view.
    saved = len(ledger.evidence_items) if ledger is not None else len(session.evidence)
    base = ResearchContextView(questions, (), attempts, recent, saved)
    if len(base.render()) > budget:
        attempts, recent = (), ()
        base = ResearchContextView(questions, (), attempts, recent, saved)
    if len(base.render()) > budget:
        omitted = tuple({"question_ref": row["question_ref"], "question_omitted": True} for row in questions)
        view = ResearchContextView(omitted, (), (), (), saved, len(rows),
            "Full question wording exceeds this view budget. Questions are omitted, not complete. "
            "Use the original research_plan receipt; do not infer omitted conditions.")
    else:
        room = budget - len(base.render()) - 100
        count = min(len(rows), max(0, room // 1000))
        per_ref = room // count - 2 if count else 1000
        view = ResearchContextView(questions, tuple(_bounded(row, per_ref) for row in rows[:count]),
                                  attempts, recent, saved, len(rows) - count)
    if len(view.render()) > budget:
        raise ValueError("research working view exceeds its serialization budget")
    return view
