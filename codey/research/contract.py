"""Research Contract Lite: deterministic completion projection.

This module projects facts that already exist (a ResearchProofReview and its
derived ReviewFindings) into the shared completion-contract primitives. Like
the rest of the evidence stack it is a projection layer: it never calls
models, fetches sources, reads Ghost state, or persists anything, and its
payloads carry only statuses, reason codes, and bounded refs.

Blocking semantics are inherited from the proof review: a review that passes
cannot have open critical findings (critical finding kinds are projections of
hard review failures), so adding the findings check to the contract cannot
flip any previously completing queued item.
"""

from __future__ import annotations

from typing import Iterable

from codey.completion.contract import (
    CHECK_FAIL,
    CHECK_PASS,
    CompletionCheck,
    CompletionContract,
    build_completion_contract,
    completion_check,
    safe_run_ref,
)
from codey.utils.refs import bounded_refs, identifier
from codey.research.proof_quality import ResearchProofReview, proof_ref_for_review
from codey.research.review_finding import (
    SEVERITY_CRITICAL,
    STATUS_OPEN,
    findings_from_proof_review,
)
from codey.research.evidence_runtime import normalize_runtime_ref


DOMAIN = "research"

CHECK_RESEARCH_PROOF = "research_proof_review"
CHECK_ANSWERS_QUESTION = "answers_question"
CHECK_SUPPORT_VERIFIED = "support_relation_verified"
CHECK_COUNTER_CHECKED = "counterevidence_checked"
CHECK_LEDGER_RECORD = "ledger_record_verified"
CHECK_BLOCKING_FINDINGS = "blocking_findings_clear"

BLOCKING_FINDINGS_REASON = "open_blocking_findings"


def research_blocked_reason(review: ResearchProofReview | None) -> str:
    """The queue gate's legacy reason codes, now owned by the projection."""

    if review is None:
        return "research_proof_missing_research_record"
    if review.missing_evidence:
        return identifier(f"research_proof_{review.missing_evidence[0]}", 120)
    if not review.answers_question:
        return "research_proof_answer_coverage_gap"
    return "research_proof_failed"


def blocking_finding_refs(findings: Iterable[object]) -> tuple[str, ...]:
    """Open critical finding refs; they block a clean complete."""

    refs: list[str] = []
    for finding in findings or ():
        severity = identifier(getattr(finding, "severity", ""), 20)
        status = identifier(getattr(finding, "status", ""), 20)
        ref = normalize_runtime_ref(
            getattr(finding, "finding_id", ""),
            kind="review_finding",
        )
        if not ref or status != STATUS_OPEN or severity != SEVERITY_CRITICAL:
            continue
        if ref not in refs:
            refs.append(ref)
    return tuple(refs)


def research_completion_checks(
    review: ResearchProofReview | None,
    *,
    findings: Iterable[object] = (),
) -> tuple[CompletionCheck, ...]:
    """Project review booleans and derived findings into contract checks."""

    ok = bool(review is not None and review.ok)
    answers = bool(review is not None and review.answers_question)
    blocking = blocking_finding_refs(findings)
    support_verified = bool(review is not None and review.support_relation_verified)
    counter_checked = bool(review is not None and review.counterevidence_checked)
    ledger_verified = bool(review is not None and review.ledger_record_verified)
    rows = [
        completion_check(
            CHECK_RESEARCH_PROOF,
            CHECK_PASS if ok else CHECK_FAIL,
            "" if ok else research_blocked_reason(review),
        ),
        completion_check(
            CHECK_ANSWERS_QUESTION,
            CHECK_PASS if answers else CHECK_FAIL,
            "" if answers else "research_proof_answer_coverage_gap",
        ),
        completion_check(
            CHECK_SUPPORT_VERIFIED,
            CHECK_PASS if support_verified else CHECK_FAIL,
            "" if support_verified else "research_proof_support_relation_unverified",
        ),
        completion_check(
            CHECK_COUNTER_CHECKED,
            CHECK_PASS if counter_checked else CHECK_FAIL,
            "" if counter_checked else "research_proof_counterevidence_missing",
        ),
        completion_check(
            CHECK_LEDGER_RECORD,
            CHECK_PASS if ledger_verified else CHECK_FAIL,
            "" if ledger_verified else "research_proof_missing_evidence_ledger_record",
        ),
        completion_check(
            CHECK_BLOCKING_FINDINGS,
            CHECK_FAIL if blocking else CHECK_PASS,
            BLOCKING_FINDINGS_REASON if blocking else "",
        ),
    ]
    valid = [row for row in rows if row is not None]
    return tuple(valid)


def research_subject_ref(research_result: object = None, event: object = None) -> str:
    synthesis = identifier(getattr(research_result, "synthesis_id", ""), 120)
    if synthesis:
        return f"research:{synthesis}"
    run_ref = safe_run_ref((event or {}).get("run_id") if isinstance(event, dict) else "")
    if run_ref:
        return f"ledger:{run_ref}"
    return DOMAIN


def research_external_refs(
    *,
    event: object,
    research_result: object = None,
    review: ResearchProofReview | None = None,
) -> tuple[str, ...]:
    run_ref = safe_run_ref(event.get("run_id") if isinstance(event, dict) else "")
    synthesis = identifier(getattr(research_result, "synthesis_id", ""), 120)
    refs: list[str] = []
    if run_ref:
        refs.append(f"ledger:{run_ref}")
    if synthesis:
        refs.append(f"research:{synthesis}")
    proof_ref = ""
    if review is not None:
        try:
            proof_ref = proof_ref_for_review(review)
        except Exception:
            proof_ref = review.proof_ref
    if proof_ref:
        refs.append(proof_ref)
    return bounded_refs(refs, limit=12)


def build_research_completion_contract(
    *,
    review: ResearchProofReview | None,
    event: object = None,
    research_result: object = None,
) -> CompletionContract | None:
    """Project one proof review into the shared contract shape."""

    findings = findings_from_proof_review(review)
    record_ref = normalize_runtime_ref(
        getattr(review, "record_id", "") if review is not None else "",
        kind="research_record",
    )
    return build_completion_contract(
        domain=DOMAIN,
        subject_ref=research_subject_ref(research_result, event),
        checks=research_completion_checks(review, findings=findings),
        evidence_refs=(record_ref,) if record_ref else (),
        finding_refs=tuple(
            normalize_runtime_ref(getattr(item, "finding_id", ""), kind="review_finding") for item in findings
        ),
        external_refs=research_external_refs(
            event=event if isinstance(event, dict) else {},
            research_result=research_result,
            review=review,
        ),
    )


__all__ = [
    "BLOCKING_FINDINGS_REASON",
    "CHECK_ANSWERS_QUESTION",
    "CHECK_BLOCKING_FINDINGS",
    "CHECK_COUNTER_CHECKED",
    "CHECK_LEDGER_RECORD",
    "CHECK_RESEARCH_PROOF",
    "CHECK_SUPPORT_VERIFIED",
    "build_research_completion_contract",
    "blocking_finding_refs",
    "research_blocked_reason",
    "research_completion_checks",
    "research_subject_ref",
]
