"""Consume accepted completion proofs; review legacy records only without one."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from codey.completion.contract import CompletionProof, project_completion_proof
from codey.research.contract import (
    build_research_completion_contract,
    research_blocked_reason,
    research_external_refs,
)
from codey.research.evidence_ledger import EvidenceLedgerStore
from codey.research.proof_quality import ResearchProofReview, review_research_proof
from codey.utils.refs import identifier

RESEARCH_QUEUE_KINDS = frozenset({"research", "open_question"})


@dataclass(frozen=True)
class ResearchCompletionDecision:
    action: str
    proof_refs: tuple[str, ...] = ()
    blocked_reason: str = ""
    review: ResearchProofReview | None = None
    proof: CompletionProof | None = None

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
        task: str | None = None,
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
                )
                or "run_not_done",
            )
        record = getattr(research_result, "research_record", None)
        accepted = getattr(research_result, "accepted_report", None)
        proof = getattr(research_result, "completion_proof", None)
        compiled_publication = isinstance(proof, CompletionProof) and any(
            row.check_id == "research_publication" for row in proof.checks)
        if accepted is not None or compiled_publication or getattr(research_result, "semantic_review", None) is not None:
            from codey.research.report_projection import validate_accepted_projection

            try:
                run_id = str(event.get("run_id") or "")
                expected_task = task if task is not None else str(getattr(item, "title", "") or "").strip()
                if (not run_id or not expected_task or research_result.question != expected_task
                        or getattr(item, "started_run_id", run_id) != run_id):
                    raise ValueError("accepted report belongs to another queued task")
                validate_accepted_projection(research_result, run_id=run_id,
                                             session_id=session_id, project=str(project or ""))
            except (ValueError, TypeError, AttributeError, KeyError):
                return ResearchCompletionDecision("block", blocked_reason="accepted_report_projection_invalid")
            proof = research_result.completion_proof
            return ResearchCompletionDecision("complete", proof=proof,
                proof_refs=(proof.proof_id, f"research:{research_result.synthesis_id}",
                            *research_external_refs(event=event)))
        question = (
            str(getattr(item, "title", "") or "").strip() or str(getattr(research_result, "question", "") or "").strip()
        )
        ledger_payload: Mapping[str, object] | None = None
        if self.evidence_ledgers is not None and accepted is None:
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
                require_ledger_record=accepted is None,
            )
        except Exception:
            review = review_research_proof(
                None,
                question=question,
                evidence_ledger=None,
                require_ledger_record=True,
            )
        proof = project_completion_proof(
            build_research_completion_contract(
                review=review,
                event=event,
                research_result=research_result,
            )
        )
        if not review.ok:
            return ResearchCompletionDecision(
                "block",
                blocked_reason=research_blocked_reason(review),
                review=review,
                proof=proof,
            )
        return ResearchCompletionDecision(
            "complete",
            proof_refs=research_external_refs(
                event=event,
                research_result=research_result,
                review=review,
            ),
            review=review,
            proof=proof,
        )


__all__ = [
    "RESEARCH_QUEUE_KINDS",
    "ResearchCompletionDecision",
    "ResearchCompletionGate",
]
