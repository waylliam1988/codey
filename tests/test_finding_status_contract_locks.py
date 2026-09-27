"""Red-first locks: findings are open-only audit snapshots.

Post-cleanup contract (must FAIL before the fix, PASS after):

- No unreachable lifecycle states or provenance fields on
  ``ReviewFindingRecord``; trace projections always emit ``open``.
- ``blocking_finding_refs`` blocks EVERY critical finding regardless of any
  hand-set status (same critical finding blocks as open AND as addressed).
- The producer-less ``failed_analysis_support -> rerun_analysis`` mapping is
  gone.
- ``_planner_warnings`` takes no ``registry`` argument.
- The event matrix no longer lists the deleted ``domain_evidence_profiles``.
"""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace


def _critical_finding(status: str) -> SimpleNamespace:
    return SimpleNamespace(
        finding_id="review_finding:" + "b" * 16,
        kind="unsupported_claim",
        severity="critical",
        status=status,
    )


def test_no_unreachable_finding_statuses_or_provenance_fields() -> None:
    import codey.research.review_finding as rf

    for name in ("STATUS_ADDRESSED", "STATUS_CONFIRMED", "STATUS_REJECTED"):
        assert not hasattr(rf, name), name
    assert rf.STATUS_OPEN == "open"
    assert frozenset({"open"}) == rf.FINDING_STATUSES
    fields = rf.ReviewFindingRecord.__dataclass_fields__
    assert "addressed_by" not in fields
    assert "confirmed_by" not in fields
    assert "status" in fields
    payload = rf.ReviewFindingRecord(
        finding_id="review_finding:" + "a" * 16,
        kind="unsupported_claim",
        severity="critical",
        target_ref="claim:" + "b" * 16,
    ).to_payload()
    assert payload["status"] == "open"
    assert "addressed_by" not in payload
    assert "confirmed_by" not in payload
    assert "STATUS_CONFIRMED" not in rf.__all__
    assert "STATUS_OPEN" in rf.__all__


def test_all_critical_findings_block_regardless_of_status() -> None:
    from codey.research.contract import blocking_finding_refs

    blocked_open = blocking_finding_refs([_critical_finding("open")])
    assert blocked_open == ("review_finding:" + "b" * 16,)
    # Same critical finding hand-marked addressed/confirmed/rejected must
    # still block: there is no lifecycle that can clear a critical finding.
    for status in ("addressed", "confirmed", "rejected"):
        assert blocking_finding_refs([_critical_finding(status)]) == blocked_open, status
    warning = SimpleNamespace(
        finding_id="review_finding:" + "c" * 16,
        kind="unsupported_claim",
        severity="warning",
        status="open",
    )
    assert blocking_finding_refs([warning]) == ()


def test_failed_analysis_support_mapping_is_gone() -> None:
    import codey.research.review_finding as rf
    from codey.research.review_finding import planner_gaps_from_findings

    assert not hasattr(rf, "FINDING_FAILED_ANALYSIS_SUPPORT")
    assert not hasattr(rf, "GAP_RERUN_ANALYSIS")
    assert "failed_analysis_support" not in rf.FINDING_KINDS
    assert "rerun_analysis" not in rf.GAP_KINDS
    assert "FINDING_FAILED_ANALYSIS_SUPPORT" not in rf.__all__
    orphan = SimpleNamespace(
        finding_id="review_finding:" + "d" * 16,
        kind="failed_analysis_support",
        severity="critical",
        status="open",
        target_ref="analysis_run:" + "e" * 16,
        proof_ref="",
        reason_codes=("failed_analysis",),
        finding_id_kind="review_finding",
    )
    assert planner_gaps_from_findings([orphan]) == ()


def test_planner_warnings_takes_no_registry() -> None:
    import inspect

    from codey.research import query_planner as qp

    assert "registry" not in inspect.signature(qp._planner_warnings).parameters
    assert qp._planner_warnings({"ok": False}, ()) == ("no_connector_preference",)
    assert qp._planner_warnings({}, ()) == (
        "missing_proof_review",
        "no_connector_preference",
    )


def test_event_matrix_no_longer_lists_deleted_profile_capability() -> None:
    root = Path(__file__).resolve().parents[1]
    text = (root / "docs" / "codey_event_matrix.md").read_text(encoding="utf-8")
    assert "domain_evidence_profiles" not in text
