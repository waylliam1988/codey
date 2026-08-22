from __future__ import annotations

from codey.research.analysis_run import analysis_run_record
from codey.research.evidence_runtime import EvidenceRuntimeSnapshot
from codey.research.identity import digest_json
from codey.research.proof_quality import ProofDiagnostic, ResearchProofReview
from codey.research.review_finding import (
    CONFIRMATION_SOURCES,
    FINDING_CITATION_MISMATCH,
    FINDING_MISSING_COUNTEREVIDENCE,
    FINDING_OVERREACH,
    FINDING_STALE_SOURCE,
    FINDING_UNSUPPORTED_CLAIM,
    PlannerGap,
    ReviewFindingEvent,
    ReviewFindingRecord,
    SEVERITY_CRITICAL,
    SEVERITY_WARNING,
    STATUS_ADDRESSED,
    STATUS_CONFIRMED,
    STATUS_OPEN,
    STATUS_REJECTED,
    apply_finding_events,
    failed_analysis_findings,
    findings_from_proof_review,
    planner_gap_trace_payloads,
    planner_gaps_from_findings,
    review_finding_trace_payloads,
)


def _stable_hex(seed: str) -> str:
    return digest_json(seed).removeprefix("sha256:")[:16]


def _claim(seed: str) -> str:
    return "claim:" + _stable_hex(seed)


def _evidence(seed: str) -> str:
    return "evidence:" + _stable_hex(seed)


def _source(seed: str) -> str:
    return "source:" + _stable_hex(seed)


def _proof_ref() -> str:
    return "research_proof:" + _stable_hex("proof")


def _record_ref() -> str:
    return "research_record:" + _stable_hex("record")


def _snapshot() -> EvidenceRuntimeSnapshot:
    return EvidenceRuntimeSnapshot(
        record_ref=_record_ref(),
        record_digest="sha256:" + digest_json("digest").removeprefix("sha256:"),
        answer_status="partial",
        proof_ref=_proof_ref(),
        source_refs=(_source("known"),),
        evidence_refs=(_evidence("known"),),
        claim_refs=(_claim("known"),),
    )


def _review(diagnostics=(), **overrides) -> ResearchProofReview:
    fields = dict(
        ok=False,
        answers_question=False,
        answer_status="partial",
        answer_coverage_score=0.5,
        citation_present=True,
        citation_locator_verified=False,
        support_relation_verified=False,
        counterevidence_checked=True,
        ledger_record_verified=True,
        question_digest="sha256:" + digest_json("q").removeprefix("sha256:"),
        missing_evidence=("partial_answer",),
        proof_ref=_proof_ref(),
        record_id=_record_ref(),
        record_digest="sha256:" + digest_json("digest").removeprefix("sha256:"),
    )
    fields.update(overrides)
    return ResearchProofReview(diagnostics=tuple(diagnostics), **fields)


def test_unsupported_claim_diagnostic_projects_critical_located_finding() -> None:
    claim = _claim("known")
    review = _review([
        ProofDiagnostic(
            reason_code="claim_missing_support_relation",
            claim_ref=claim,
        ),
        ProofDiagnostic(reason_code="claim_not_evidence_backed", claim_ref=claim),
    ])

    findings = findings_from_proof_review(review, _snapshot())

    assert len(findings) == 1
    finding = findings[0]
    assert finding.kind == FINDING_UNSUPPORTED_CLAIM
    assert finding.severity == SEVERITY_CRITICAL
    assert finding.status == STATUS_OPEN
    assert finding.claim_ref == claim
    assert finding.target_ref == claim
    assert finding.proof_ref == _proof_ref()
    assert finding.reason_codes == (
        "claim_missing_support_relation",
        "claim_not_evidence_backed",
    )
    # Same inputs must project to the same stable id.
    again = findings_from_proof_review(review, _snapshot())
    assert again[0].finding_id == finding.finding_id


def test_citation_and_support_diagnostics_project_warning_findings() -> None:
    claim = _claim("known")
    evidence = _evidence("unknown")
    review = _review([
        ProofDiagnostic(reason_code="claim_missing_citation", claim_ref=claim),
        ProofDiagnostic(
            reason_code="support_relation_bad_locator",
            claim_ref=claim,
            evidence_ref=evidence,
            source_ref=_source("known"),
        ),
    ])

    findings = findings_from_proof_review(review)

    by_reason = {
        reason: finding
        for finding in findings
        for reason in finding.reason_codes
    }
    locator_finding = by_reason["support_relation_bad_locator"]
    assert locator_finding.kind == FINDING_CITATION_MISMATCH
    assert locator_finding.severity == SEVERITY_WARNING
    assert locator_finding.evidence_ref == evidence
    assert locator_finding.source_ref == _source("known")
    assert by_reason["claim_missing_citation"].kind == FINDING_CITATION_MISMATCH


def test_snapshot_filtering_drops_refs_outside_the_record_graph() -> None:
    ghost_claim = _claim("ghost")
    known_claim = _claim("known")
    review = _review([
        ProofDiagnostic(reason_code="claim_not_evidence_backed", claim_ref=ghost_claim),
        ProofDiagnostic(reason_code="claim_missing_citation", claim_ref=known_claim),
    ])

    filtered = findings_from_proof_review(review, _snapshot())
    unfiltered = findings_from_proof_review(review)

    assert all(finding.claim_ref != ghost_claim for finding in filtered)
    assert any(finding.claim_ref == known_claim for finding in filtered)
    assert any(finding.claim_ref == ghost_claim for finding in unfiltered)


def test_record_scoped_warnings_project_findings_anchored_on_the_proof() -> None:
    review = _review(
        missing_evidence=("partial_answer", "counterevidence_not_checked"),
        stale_warnings=("sources_stale_or_undated",),
        overclaim_warnings=("strong_claim_without_support",),
    )

    findings = findings_from_proof_review(review, _snapshot())

    kinds = {finding.kind: finding for finding in findings}
    assert set(kinds) == {
        FINDING_MISSING_COUNTEREVIDENCE,
        FINDING_STALE_SOURCE,
        FINDING_OVERREACH,
    }
    for finding in kinds.values():
        assert finding.target_ref in {_proof_ref(), _record_ref()}
    assert kinds[FINDING_MISSING_COUNTEREVIDENCE].reason_codes == ("counterevidence_not_checked",)
    assert kinds[FINDING_MISSING_COUNTEREVIDENCE].severity == SEVERITY_WARNING


def test_findings_without_any_anchor_are_skipped() -> None:
    review = _review(proof_ref="", record_id="")
    review_with_diag = _review(
        [ProofDiagnostic(reason_code="claim_missing_citation")],
        proof_ref="",
        record_id="",
    )

    assert findings_from_proof_review(None) == ()
    assert findings_from_proof_review(review) == ()
    assert findings_from_proof_review(review_with_diag) == ()


def test_failed_analysis_runs_project_support_findings() -> None:
    failed = analysis_run_record({
        "command": "python script.py",
        "tool_id": "1:1",
        "tool_name": "run",
        "ok": False,
        "exit_code": 2,
    })
    ok_run = analysis_run_record({
        "command": "python good.py",
        "tool_id": "1:2",
        "tool_name": "run",
        "ok": True,
        "exit_code": 0,
    })
    assert failed is not None and ok_run is not None

    findings = failed_analysis_findings([failed, ok_run, {"analysis_run_id": "junk"}, None])

    assert len(findings) == 1
    finding = findings[0]
    assert finding.kind == "failed_analysis_support"
    assert finding.severity == SEVERITY_CRITICAL
    assert finding.analysis_run_ref == failed.analysis_run_id
    assert finding.target_ref == failed.analysis_run_id
    assert finding.reason_codes == ("failed_analysis",)


def test_planner_gaps_map_each_actionable_finding_kind() -> None:
    def finding(kind: str, target: str) -> ReviewFindingRecord:
        return ReviewFindingRecord(
            finding_id="review_finding:" + _stable_hex(kind + target),
            kind=kind,
            severity=SEVERITY_WARNING,
            target_ref=target,
            reason_codes=("reason_" + kind,),
        )

    findings = [
        finding(FINDING_UNSUPPORTED_CLAIM, _claim("known")),
        finding(FINDING_CITATION_MISMATCH, _claim("known")),
        finding(FINDING_MISSING_COUNTEREVIDENCE, _proof_ref()),
        finding(FINDING_STALE_SOURCE, _proof_ref()),
        finding(FINDING_OVERREACH, _proof_ref()),
        finding("qualified_support", _proof_ref()),
        finding("contradictory_sources", _proof_ref()),
    ]

    gaps = planner_gaps_from_findings(findings)
    by_kind = {}
    for gap in gaps:
        by_kind.setdefault(gap.gap_kind, []).append(gap)

    assert by_kind["followup_search"][0].target_ref == _claim("known")
    assert by_kind["locator_verification"][0].target_ref == _claim("known")
    assert by_kind["counterevidence_search"][0].target_ref == _proof_ref()
    assert by_kind["refresh_query"][0].target_ref == _proof_ref()
    assert all(gap.finding_refs and gap.finding_refs[0].startswith("review_finding:") for gap in gaps)
    # qualified_support / contradictory_sources have no v1 gap producer.
    assert sum(len(items) for items in by_kind.values()) == len(gaps)
    assert len(gaps) == 5
    assert all(gap.gap_id.startswith("planner_gap:") for gap in gaps)


def test_planner_gap_dedupe_and_limit() -> None:
    same_target = _claim("known")
    findings = [
        ReviewFindingRecord(
            finding_id="review_finding:" + _stable_hex(f"dup{i}"),
            kind=FINDING_CITATION_MISMATCH,
            severity=SEVERITY_WARNING,
            target_ref=same_target,
        )
        for i in range(3)
    ] + [
        ReviewFindingRecord(
            finding_id="review_finding:" + _stable_hex(f"fill{i}"),
            kind=FINDING_STALE_SOURCE,
            severity=SEVERITY_WARNING,
            target_ref=_proof_ref(),
        )
        for i in range(30)
    ]

    gaps = planner_gaps_from_findings(findings)

    assert len([gap for gap in gaps if gap.target_ref == same_target]) == 1
    assert len(gaps) <= 16


def test_apply_finding_events_requires_verification_for_confirmed() -> None:
    target = _claim("known")
    finding = ReviewFindingRecord(
        finding_id="review_finding:" + _stable_hex("lifecycle"),
        kind=FINDING_UNSUPPORTED_CLAIM,
        severity=SEVERITY_CRITICAL,
        target_ref=target,
        claim_ref=target,
    )

    self_reported = apply_finding_events([finding], [
        ReviewFindingEvent(action="confirmed", finding_ref=finding.finding_id, verified_by="model_said_fixed"),
    ])
    assert self_reported[0].status == STATUS_OPEN

    confirmed = apply_finding_events(self_reported, [
        ReviewFindingEvent(
            action="confirmed",
            finding_ref=finding.finding_id,
            verified_by=sorted(CONFIRMATION_SOURCES)[0],
        ),
    ])
    assert confirmed[0].status == STATUS_CONFIRMED
    assert confirmed[0].confirmed_by == (sorted(CONFIRMATION_SOURCES)[0],)


def test_apply_finding_events_lifecycle_and_no_ops() -> None:
    finding = ReviewFindingRecord(
        finding_id="review_finding:" + _stable_hex("flow"),
        kind=FINDING_STALE_SOURCE,
        severity=SEVERITY_WARNING,
        target_ref=_proof_ref(),
    )

    addressed = apply_finding_events([finding], [
        ReviewFindingEvent(action="addressed", finding_ref=finding.finding_id, reason_code="refresh_done"),
    ])
    assert addressed[0].status == STATUS_ADDRESSED
    assert addressed[0].addressed_by == ("refresh_done",)

    rejected = apply_finding_events([finding], [
        ReviewFindingEvent(action="rejected", finding_ref=finding.finding_id),
    ])
    assert rejected[0].status == STATUS_REJECTED

    untouched = apply_finding_events([finding], [
        ReviewFindingEvent(action="bogus", finding_ref=finding.finding_id),
        ReviewFindingEvent(action="confirmed", finding_ref="review_finding:" + _stable_hex("other")),
    ])
    assert untouched == (finding,)
    # Order is preserved even when only some findings change.
    other = ReviewFindingRecord(
        finding_id="review_finding:" + _stable_hex("other"),
        kind=FINDING_OVERREACH,
        severity=SEVERITY_WARNING,
        target_ref=_proof_ref(),
    )
    mixed = apply_finding_events([finding, other], [
        ReviewFindingEvent(action="addressed", finding_ref=other.finding_id),
    ])
    assert [row.finding_id for row in mixed] == [finding.finding_id, other.finding_id]
    assert mixed[0].status == STATUS_OPEN
    assert mixed[1].status == STATUS_ADDRESSED


def test_review_finding_record_has_no_freeform_message_field() -> None:
    assert "message" not in ReviewFindingRecord.__dataclass_fields__


def test_trace_payloads_keep_refs_only_and_drop_invalid_entries() -> None:
    finding = ReviewFindingRecord(
        finding_id="review_finding:" + _stable_hex("trace"),
        kind=FINDING_UNSUPPORTED_CLAIM,
        severity=SEVERITY_CRITICAL,
        target_ref=_claim("known"),
        claim_ref=_claim("known"),
        proof_ref=_proof_ref(),
        reason_codes=("claim_missing_support_relation",),
    )
    gaps = [
        PlannerGap(
            gap_id="planner_gap:" + _stable_hex("trace"),
            gap_kind="followup_search",
            target_ref=_claim("known"),
            reason_codes=("claim_missing_support_relation",),
            finding_refs=(finding.finding_id, "https://evil.example/nope"),
        ),
        PlannerGap(gap_id="planner_gap:zzz", gap_kind="bogus_gap"),
    ]

    raw_mapping_ref = "review_finding:" + _stable_hex("raw-message")
    finding_payloads = review_finding_trace_payloads([
        finding,
        {"finding_id": "nope"},
        {
            "finding_id": raw_mapping_ref,
            "kind": FINDING_STALE_SOURCE,
            "severity": "urgent",
            "status": "model_fixed",
            "target_ref": _source("known"),
            "message": "RAW MESSAGE SHOULD NOT BE SAVED",
        },
        {
            "finding_id": "review_finding:" + _stable_hex("bad-kind"),
            "kind": "made_up_finding",
        },
        "junk",
    ])
    gap_payloads = planner_gap_trace_payloads(gaps)

    assert len(finding_payloads) == 2
    payload = finding_payloads[0]
    raw_payload = finding_payloads[1]
    assert payload["finding_id"] == finding.finding_id
    assert payload["claim_ref"] == finding.claim_ref
    assert raw_payload["finding_id"] == raw_mapping_ref
    assert raw_payload["severity"] == SEVERITY_WARNING
    assert raw_payload["status"] == STATUS_OPEN
    assert "message" not in raw_payload
    assert "RAW MESSAGE" not in repr(finding_payloads)
    assert len(gap_payloads) == 1
    assert gap_payloads[0]["finding_refs"] == [finding.finding_id]
    assert gap_payloads[0]["gap_kind"] == "followup_search"
