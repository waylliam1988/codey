"""Pure follow-up selection decisions for Research.

This module owns deterministic candidate ranking and follow-up stop reasons.
It deliberately consumes duck-typed result/review/plan objects so the live
pipeline, manual harnesses, and release gates can share one decision core
without importing providers, tools, storage, or runner code.
"""

from __future__ import annotations

import math
from collections.abc import Iterable
from dataclasses import dataclass


@dataclass(frozen=True)
class ResearchCandidateScore:
    """Explicit lexicographic ranking for follow-up candidate selection.

    Field order is the priority order:

    1. ``proof_rank``: a proof-complete result dominates partial answers.
    2. ``stop_rank``: clean done beats budget stops beats abnormal stops.
    3. ``answers_question``: question alignment before evidence volume.
    4. ``coverage``: bounded finite answer-coverage score.
    5-7. Verification booleans: citation locator, support relation,
       counterevidence.
    8. ``missing_evidence_count``: fewer unsupported gaps wins ties.

    Unsupported-claim regression is a separate dominance constraint in
    :func:`selects_candidate`; no score weight can buy it back.
    """

    proof_rank: float
    stop_rank: float
    answers_question: bool
    coverage: float
    citation_locator_verified: bool
    support_relation_verified: bool
    counterevidence_checked: bool
    missing_evidence_count: int

    def sort_key(self) -> tuple[float, float, int, float, int, int, int, int]:
        return (
            self.proof_rank,
            self.stop_rank,
            int(self.answers_question),
            self.coverage,
            int(self.citation_locator_verified),
            int(self.support_relation_verified),
            int(self.counterevidence_checked),
            -self.missing_evidence_count,
        )


def selects_candidate(
    candidate: object,
    candidate_review: object | None,
    current: object,
    current_review: object | None,
) -> bool:
    if getattr(candidate, "stop_reason", "") in {"error", "protocol", "stopped"}:
        return False
    if candidate_review is None:
        return False
    if unsupported_regression(candidate_review, current_review):
        return False
    return (
        candidate_score(candidate, candidate_review).sort_key()
        > candidate_score(current, current_review).sort_key()
    )


def candidate_score(result: object, review: object | None) -> ResearchCandidateScore:
    if review is None:
        return ResearchCandidateScore(
            proof_rank=0.0,
            stop_rank=stop_score(getattr(result, "stop_reason", "")),
            answers_question=False,
            coverage=0.0,
            citation_locator_verified=False,
            support_relation_verified=False,
            counterevidence_checked=False,
            missing_evidence_count=0,
        )
    return ResearchCandidateScore(
        proof_rank=(
            4.0
            if getattr(review, "ok", False)
            else float(answer_status_rank(getattr(review, "answer_status", "")))
        ),
        stop_rank=stop_score(getattr(result, "stop_reason", "")),
        answers_question=bool(getattr(review, "answers_question", False)),
        coverage=bounded_score(getattr(review, "answer_coverage_score", 0.0)),
        citation_locator_verified=bool(getattr(review, "citation_locator_verified", False)),
        support_relation_verified=bool(getattr(review, "support_relation_verified", False)),
        counterevidence_checked=bool(getattr(review, "counterevidence_checked", False)),
        missing_evidence_count=len(tuple_values(getattr(review, "missing_evidence", ()))),
    )


def unsupported_regression(candidate_review: object, current_review: object | None) -> bool:
    if current_review is None:
        return False
    current_unsupported = "unsupported_claims" in set(tuple_values(getattr(current_review, "missing_evidence", ())))
    candidate_unsupported = "unsupported_claims" in set(tuple_values(getattr(candidate_review, "missing_evidence", ())))
    return candidate_unsupported and not current_unsupported


def has_actionable_gap(review: object | None) -> bool:
    if review is None:
        return False
    if getattr(review, "ok", False):
        return False
    answer_status = str(getattr(review, "answer_status", "") or "")
    if answer_status in {"not_answered", "insufficient_evidence"}:
        return True
    missing = set(tuple_values(getattr(review, "missing_evidence", ())))
    return bool(
        answer_status == "partial"
        and (
            tuple_values(getattr(review, "coverage_gaps", ()))
            or tuple_values(getattr(review, "followup_questions", ()))
            or tuple_values(getattr(review, "query_rewrite_candidates", ()))
            or missing.intersection({
                "answer_coverage_gap",
                "counterevidence_not_checked",
                "partial_answer",
            })
        )
    )


def is_followup_eligible_stop(stop_reason: object) -> bool:
    reason = str(stop_reason or "").strip().lower()
    return reason not in {"stopped", "cancelled", "user_aborted", "timeout"}


def pipeline_stop_reason(plan: object, review: object | None = None) -> str:
    if not tuple_values(getattr(plan, "query_candidates", ())):
        reasons = set(tuple_values(getattr(plan, "reason_codes", ())))
        if "proof_ok_no_required_followup" in reasons:
            return "proof_ok_no_required_followup"
        return "no_query_candidates"
    if not has_actionable_gap(review):
        return "no_actionable_gap"
    return "planned"


def answer_status_rank(status: object) -> int:
    return {
        "answered": 3,
        "partial": 2,
        "insufficient_evidence": 1,
        "not_answered": 0,
    }.get(str(status or ""), 0)


def bounded_score(value: object) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return 0.0
    if not math.isfinite(parsed):
        return 0.0
    return max(0.0, min(1.0, parsed))


def stop_score(stop_reason: object) -> float:
    if stop_reason == "done":
        return 1.0
    if stop_reason in {"max_turns", "no_progress"}:
        return 0.4
    return 0.0


def tuple_values(value: object) -> tuple[object, ...]:
    if value is None:
        return ()
    if isinstance(value, str):
        return (value,)
    if isinstance(value, Iterable):
        return tuple(value)
    return (value,)


__all__ = [
    "ResearchCandidateScore",
    "answer_status_rank",
    "bounded_score",
    "candidate_score",
    "has_actionable_gap",
    "is_followup_eligible_stop",
    "pipeline_stop_reason",
    "selects_candidate",
    "stop_score",
    "unsupported_regression",
]
