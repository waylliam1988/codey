"""Bounded diagnostics used in research trace projection."""
import pytest

from codey.research.audit_result import MAX_AUDIT_DIAGNOSTICS, AuditDiagnostic, AuditOutcome
from codey.runs.trace_research import project_audit_diagnostics, project_research_pipeline_result


def test_diagnostic_projection_is_bounded_and_rejects_invalid_rows():
    valid = {"stage": "proof_review", "status": "error", "phase": "final", "reason_code": "failed"}
    invalid = [None, {}, {**valid, "phase": "unknown"}, {**valid, "status": "unknown"}, {**valid, "reason_code": ""}]
    rows = project_audit_diagnostics(invalid + [{**valid, "value": "private", "message": "private"}] * 50)
    assert len(rows) == MAX_AUDIT_DIAGNOSTICS
    assert all(set(row) == {"stage", "status", "phase", "reason_code", "error_type"} for row in rows)
    assert "audit_diagnostics" not in project_research_pipeline_result({})
    assert project_research_pipeline_result({"audit_diagnostics": None})["audit_diagnostics"] == []
    clipped = project_audit_diagnostics([{**valid, "stage": "x" * 100, "error_type": "E" * 100}])[0]
    assert len(clipped["stage"]) == len(clipped["error_type"]) == 80


def test_outcome_contract_distinguishes_empty_result_from_missing_result():
    diagnostic = AuditDiagnostic("findings", "ok", "final")
    assert AuditOutcome(diagnostic, ()).value == ()
    with pytest.raises(ValueError, match="carry a result"):
        AuditOutcome(diagnostic)
    skipped = AuditDiagnostic("findings", "skipped", "final", "snapshot_unavailable")
    with pytest.raises(ValueError, match="carry a result"):
        AuditOutcome(skipped, ())
    with pytest.raises(ValueError, match="require a reason"):
        AuditDiagnostic("findings", "error", "final")
