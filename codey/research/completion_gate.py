"""Research completion gate for local queued work items."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from codey.research.evidence_ledger import EvidenceLedgerStore
from codey.research.identity import bounded_refs, digest_text, identifier
from codey.research.proof_quality import ResearchProofReview, review_research_proof
from codey.research.redaction import looks_sensitive_signal


RESEARCH_QUEUE_KINDS = frozenset({"research", "open_question"})


@dataclass(frozen=True)
class ResearchCompletionDecision:
    action: str
    proof_refs: tuple[str, ...] = ()
    blocked_reason: str = ""
    review: ResearchProofReview | None = None

    @property
    def complete(self) -> bool:
        return self.action == "complete"

    @property
    def block(self) -> bool:
        return self.action == "block"

    @property
    def no_signal(self) -> bool:
        return self.action == "no_signal"


class ResearchCompletionGate:
    def __init__(self, evidence_ledgers: EvidenceLedgerStore | None = None) -> None:
        self.evidence_ledgers = evidence_ledgers

    def evaluate(
        self,
        *,
        item: Any,
        event: Mapping[str, object] | None,
        research_result: Any = None,
        session_id: str = "",
        project: str | Path | None = None,
    ) -> ResearchCompletionDecision:
        kind = identifier(getattr(item, "kind", ""), 40)
        if kind not in RESEARCH_QUEUE_KINDS:
            return ResearchCompletionDecision("no_signal")
        if not isinstance(event, Mapping) or str(event.get("stop_reason") or "") != "done":
            return ResearchCompletionDecision(
                "block",
                blocked_reason=identifier(
                    (event or {}).get("stop_reason") if isinstance(event, Mapping) else "",
                    80,
                ) or "run_not_done",
            )
        record = getattr(research_result, "research_record", None)
        question = (
            str(getattr(item, "title", "") or "").strip()
            or str(getattr(research_result, "question", "") or "").strip()
        )
        ledger_payload: Mapping[str, object] | None = None
        if self.evidence_ledgers is not None:
            try:
                snapshot = self.evidence_ledgers.load(session_id=session_id, project=project)
                if snapshot.available:
                    ledger_payload = snapshot.payload
            except Exception:
                ledger_payload = None
        try:
            review = review_research_proof(
                record,
                question=question,
                evidence_ledger=ledger_payload,
                require_ledger_record=True,
            )
        except Exception:
            review = review_research_proof(
                None,
                question=question,
                evidence_ledger=None,
                require_ledger_record=True,
            )
        if not review.ok:
            return ResearchCompletionDecision(
                "block",
                blocked_reason=_blocked_reason(review),
                review=review,
            )
        return ResearchCompletionDecision(
            "complete",
            proof_refs=_proof_refs(
                event=event,
                research_result=research_result,
                proof_ref=review.proof_ref,
            ),
            review=review,
        )


def _blocked_reason(review: ResearchProofReview) -> str:
    if review.missing_evidence:
        return identifier(f"research_proof_{review.missing_evidence[0]}", 120)
    if not review.answers_question:
        return "research_proof_answer_coverage_gap"
    return "research_proof_failed"


def _proof_refs(
    *,
    event: Mapping[str, object],
    research_result: Any,
    proof_ref: str,
) -> tuple[str, ...]:
    refs: list[str] = []
    run_ref = _safe_run_ref(event.get("run_id"))
    if run_ref:
        refs.append(f"ledger:{run_ref}")
    synthesis_id = identifier(getattr(research_result, "synthesis_id", ""), 120)
    if synthesis_id:
        refs.append(f"research:{synthesis_id}")
    if proof_ref:
        refs.append(proof_ref)
    return bounded_refs(refs, limit=12)


def _safe_run_ref(value: object) -> str:
    text = identifier(value, 120)
    if not text:
        return ""
    if looks_sensitive_signal(text):
        return digest_text(text).removeprefix("sha256:")[:16]
    return text


__all__ = [
    "RESEARCH_QUEUE_KINDS",
    "ResearchCompletionDecision",
    "ResearchCompletionGate",
]
