from __future__ import annotations

from codey.completion.contract import (
    CHECK_FAIL,
    CHECK_PASS,
    COMPLETION_COMPLETE,
    COMPLETION_FAILED,
    CompletionCheck,
    project_completion_proof,
)
from codey.research.contract import (
    BLOCKING_FINDINGS_REASON,
    CHECK_ANSWERS_QUESTION,
    CHECK_BLOCKING_FINDINGS,
    CHECK_COUNTER_CHECKED,
    CHECK_LEDGER_RECORD,
    CHECK_RESEARCH_PROOF,
    CHECK_SUPPORT_VERIFIED,
    blocking_finding_refs,
    build_research_completion_contract,
    research_blocked_reason,
    research_completion_checks,
)
from codey.research.proof_quality import ProofDiagnostic, ResearchProofReview
from codey.research.review_finding import (
    FINDING_UNSUPPORTED_CLAIM,
    SEVERITY_CRITICAL,
    SEVERITY_WARNING,
    STATUS_ADDRESSED,
    STATUS_OPEN,
    findings_from_proof_review,
)


def _review(**overrides) -> ResearchProofReview:
    fields = dict(
        ok=False,
        answers_question=False,
        answer_status="not_answered",
        answer_coverage_score=0.0,
        citation_present=False,
        citation_locator_verified=False,
        support_relation_verified=False,
        counterevidence_checked=False,
        ledger_record_verified=False,
        question_digest="sha256:" + "0" * 64,
        record_id="research_record:" + "a" * 16,
        record_digest="sha256:" + "1" * 64,
    )
    fields.update(overrides)
    return ResearchProofReview(**fields)


def _check_map(checks: tuple[CompletionCheck, ...]) -> dict[str, CompletionCheck]:
    return {row.check_id: row for row in checks}


def test_missing_review_fails_closed_to_legacy_reason() -> None:
    assert research_blocked_reason(None) == "research_proof_missing_research_record"
    checks = _check_map(research_completion_checks(None))
    assert checks[CHECK_RESEARCH_PROOF].status == CHECK_FAIL
    assert checks[CHECK_RESEARCH_PROOF].reason_code == "research_proof_missing_research_record"


def test_blocked_reason_matches_queue_gate_reason_codes() -> None:
    # Legacy gate strings, now owned by the projection.
    assert research_blocked_reason(_review(missing_evidence=("missing_research_record",))) == (
        "research_proof_missing_research_record"
    )
    assert research_blocked_reason(
        _review(
            missing_evidence=("missing_evidence_ledger_record",),
            answers_question=True,
            answer_status="answered",
            answer_coverage_score=0.9,
        )
    ) == "research_proof_missing_evidence_ledger_record"
    assert research_blocked_reason(_review(answers_question=False)) == (
        "research_proof_answer_coverage_gap"
    )
    fully_ok = _review(
        ok=True,
        answers_question=True,
        answer_status="answered",
        answer_coverage_score=1.0,
        citation_present=True,
        citation_locator_verified=True,
        support_relation_verified=True,
        counterevidence_checked=True,
        ledger_record_verified=True,
    )
    assert research_blocked_reason(fully_ok) == "research_proof_failed"


def test_checks_mirror_review_booleans() -> None:
    failing = _review(
        missing_evidence=("unsupported_claims",),
        support_relation_verified=True,
        ledger_record_verified=True,
    )
    rows = _check_map(research_completion_checks(failing))

    assert rows[CHECK_RESEARCH_PROOF].status == CHECK_FAIL
    assert rows[CHECK_RESEARCH_PROOF].reason_code == "research_proof_unsupported_claims"
    assert rows[CHECK_ANSWERS_QUESTION].status == CHECK_FAIL
    assert rows[CHECK_SUPPORT_VERIFIED].status == CHECK_PASS
    assert rows[CHECK_COUNTER_CHECKED].status == CHECK_FAIL
    assert rows[CHECK_LEDGER_RECORD].status == CHECK_PASS
    assert rows[CHECK_BLOCKING_FINDINGS].status == CHECK_PASS

    passing = _review(
        ok=True,
        answers_question=True,
        answer_status="answered",
        answer_coverage_score=1.0,
        citation_present=True,
        citation_locator_verified=True,
        support_relation_verified=True,
        counterevidence_checked=True,
        ledger_record_verified=True,
    )
    ok_rows = _check_map(research_completion_checks(passing))
    assert all(row.status == CHECK_PASS for row in ok_rows.values())


def test_first_failed_check_carries_the_gate_blocked_reason() -> None:
    review = _review(missing_evidence=("partial_answer",))
    proof = project_completion_proof(
        build_research_completion_contract(review=review, event={})
    )

    assert proof is not None
    # The proof review actually ran and failed, so the honest status is
    # failed; the queue gate still answers "block" with the same legacy
    # reason code it always used.
    assert proof.status == COMPLETION_FAILED
    assert not proof.satisfied
    assert proof.blocked_reason == "research_proof_partial_answer"
    assert research_blocked_reason(review) == "research_proof_partial_answer"


def test_ok_review_projects_to_complete() -> None:
    review = _review(
        ok=True,
        answers_question=True,
        answer_status="answered",
        answer_coverage_score=1.0,
        citation_present=True,
        citation_locator_verified=True,
        support_relation_verified=True,
        counterevidence_checked=True,
        ledger_record_verified=True,
    )
    proof = project_completion_proof(
        build_research_completion_contract(review=review, event={"run_id": "run-9"})
    )

    assert proof is not None
    assert proof.status == COMPLETION_COMPLETE
    assert proof.satisfied is True
    assert "ledger:run-9" in proof.external_refs
    assert f"research_record:{'a' * 16}" in proof.evidence_refs


def test_critical_open_findings_block_but_addressed_ones_do_not() -> None:
    critical = findings_from_proof_review(_review(
        diagnostics=(ProofDiagnostic(
            reason_code="claim_not_evidence_backed",
            claim_ref="claim:" + "b" * 16,
        ),),
    ))
    refs = blocking_finding_refs(critical)
    assert refs, "critical unsupported-claim diagnostic must produce an open finding"

    rows = _check_map(research_completion_checks(_review(), findings=critical))
    assert rows[CHECK_BLOCKING_FINDINGS].status == CHECK_FAIL
    assert rows[CHECK_BLOCKING_FINDINGS].reason_code == BLOCKING_FINDINGS_REASON

    addressed = tuple(
        type(finding)(**{**finding.__dict__, "status": STATUS_ADDRESSED})
        for finding in critical
    )
    assert blocking_finding_refs(addressed) == ()

    warning_only = (
        type(critical[0])(
            finding_id="review_finding:" + "c" * 16,
            kind=FINDING_UNSUPPORTED_CLAIM,
            severity=SEVERITY_WARNING,
            status=STATUS_OPEN,
        ),
    )
    assert blocking_finding_refs(warning_only) == ()


def test_ok_reviews_cannot_have_open_critical_findings() -> None:
    # Structural parity guarantee for queued completion behavior: every
    # critical finding kind is a projection of a hard proof-review failure,
    # so an ok review must derive zero open critical findings.
    critical_reasons = {
        "claim_not_evidence_backed",
        "claim_missing_support_relation",
        "support_relation_not_claim_evidence",
        "assumption_used_as_answer",
    }
    for reason in sorted(critical_reasons):
        review = _review(diagnostics=(ProofDiagnostic(reason_code=reason),))
        assert review.ok is False, reason
        findings = findings_from_proof_review(review)
        assert any(
            finding.severity == SEVERITY_CRITICAL and finding.status == STATUS_OPEN
            for finding in findings
        ), reason
        assert blocking_finding_refs(findings)
        proof = project_completion_proof(
            build_research_completion_contract(review=review, event={})
        )
        assert proof is not None and not proof.satisfied, reason


def test_contract_payload_stays_refs_only() -> None:
    review = _review(missing_evidence=("missing_research_record",))
    contract = build_research_completion_contract(
        review=review,
        event={"run_id": "run-with ?token=SECRET"},
        research_result=None,
    )
    payload = str(contract)

    assert "SECRET" not in payload
    assert contract is not None
    # A secret-looking run id keeps only its digest ref.
    external = [ref for ref in contract.external_refs if ref.startswith("ledger:")]
    assert len(external) == 1
    suffix = external[0].split(":", 1)[1]
    assert all(char in "0123456789abcdef" for char in suffix) and len(suffix) == 16
