"""Red-first locks: open-only is a type constraint, not a projection rule.

Post-cleanup contract (must FAIL before the fix, PASS after):

- ``ReviewFindingRecord`` has no ``status`` field at all: passing
  ``status=`` raises ``TypeError``, so the object can never disagree with
  its payload (previously ``confirmed`` in / ``open`` out).
- The single-value ``FINDING_STATUSES`` set is gone; ``STATUS_OPEN`` stays
  as the fixed payload marker.
- ``to_payload()`` and ``review_finding_trace_payloads`` always emit
  ``"open"``, even for mapping inputs carrying non-open statuses.
- Completion blocking ignores status entirely (mock objects with non-open
  statuses still block when critical).
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest


def _mock_finding(status: str, severity: str = "critical") -> SimpleNamespace:
    return SimpleNamespace(
        finding_id="review_finding:" + "b" * 16,
        kind="unsupported_claim",
        severity=severity,
        status=status,
    )


def test_record_type_has_no_status_field() -> None:
    from codey.research.review_finding import ReviewFindingRecord

    assert "status" not in ReviewFindingRecord.__dataclass_fields__
    with pytest.raises(TypeError):
        ReviewFindingRecord(  # type: ignore[call-arg]
            finding_id="review_finding:" + "a" * 16,
            kind="unsupported_claim",
            severity="critical",
            status="confirmed",
        )


def test_single_value_status_set_is_gone_marker_stays() -> None:
    import codey.research.review_finding as rf

    assert not hasattr(rf, "FINDING_STATUSES")
    assert rf.STATUS_OPEN == "open"
    assert "FINDING_STATUSES" not in rf.__all__
    assert "STATUS_OPEN" in rf.__all__


def test_payloads_are_fixed_open_for_non_open_inputs() -> None:
    from codey.research.review_finding import (
        ReviewFindingRecord,
        review_finding_trace_payloads,
    )

    record = ReviewFindingRecord(
        finding_id="review_finding:" + "a" * 16,
        kind="unsupported_claim",
        severity="critical",
        target_ref="claim:" + "b" * 16,
    )
    assert record.to_payload()["status"] == "open"
    for status in ("addressed", "confirmed", "rejected", "model_fixed"):
        (payload,) = review_finding_trace_payloads([{
            "finding_id": "review_finding:" + "c" * 16,
            "kind": "unsupported_claim",
            "severity": "critical",
            "status": status,
            "target_ref": "claim:" + "b" * 16,
        }])
        assert payload["status"] == "open", status


def test_blocking_ignores_status_via_mocks() -> None:
    from codey.research.contract import blocking_finding_refs

    expected = ("review_finding:" + "b" * 16,)
    for status in ("open", "addressed", "confirmed", "rejected", "model_fixed"):
        assert blocking_finding_refs([_mock_finding(status)]) == expected, status
    assert blocking_finding_refs([_mock_finding("open", severity="warning")]) == ()
