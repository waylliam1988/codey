"""Audit execution failures remain visible without changing publication gates."""

import json
import logging
from dataclasses import replace
from unittest.mock import patch

import pytest

from codey.operations.research_flow import research_payload
from codey.research import pipeline
from codey.research.audit_result import MAX_AUDIT_DIAGNOSTICS, AuditDiagnostic, AuditOutcome
from codey.research.context import ResearchContext, RunTraceResearchSink
from codey.research.evidence_ledger import EvidenceLedgerSnapshot, EvidenceLedgerStore
from codey.research.run_result import ResearchRunResult
from codey.runs.trace import RunTraceStore
from codey.runs.trace_research import project_audit_diagnostics, project_research_pipeline_result
from tests.test_research_pipeline import _allow_policy, _PipelineSearch, _record, _TraceRecorder


def make_runner(tmp_path, *, accepted=True, record=True, ledger=None, trace=None):
    result = ResearchRunResult(
        question="Pipeline question", summary="Saved answer", stop_reason="done", turns=1,
        research_record=_record(question="Pipeline question", synthesis_id="answer", project=tmp_path) if record else None,
        accepted_report=object() if accepted else None,
    )
    search = _PipelineSearch()
    runner = pipeline.ResearchPipeline(
        context=ResearchContext(question=result.question, session_id="s", run_id="r", trace=trace or _TraceRecorder()),
        run_iteration=lambda **_: pipeline.ResearchIterationRun(result),
        search_factory=lambda: search, evidence_ledgers=ledger, policy=_allow_policy(),
    )
    return runner, result, search


@pytest.mark.parametrize("failure", [False, True])
def test_review_unavailable_is_error_and_reused(tmp_path, caplog, failure):
    runner, result, search = make_runner(tmp_path)
    with patch.object(
        pipeline, "review_research_proof", return_value=None,
        side_effect=ValueError("private diagnostic") if failure else None,
    ) as review, caplog.at_level(logging.ERROR):
        output = runner.run()
    assert output.final_result is result and search.closed
    assert review.call_count == 1
    rows = [d for d in output.audit_diagnostics if d.stage == "proof_review"]
    assert len(rows) == 1
    assert rows[0].status == "error" and rows[0].phase == "initial"
    assert rows[0].reason_code == ("proof_review_failed" if failure else "invalid_proof_review_result")
    assert rows[0].error_type == ("ValueError" if failure else "")
    assert caplog.records
    if failure:
        assert caplog.records[0].exc_info is not None
    assert "private diagnostic" not in json.dumps(output.to_payload())


def test_no_record_is_skipped_without_error_log(tmp_path, caplog):
    runner, _, _ = make_runner(tmp_path, record=False)
    with patch.object(pipeline, "review_research_proof") as review:
        output = runner.run()
    review.assert_not_called()
    diagnostic = output.audit_diagnostics[0]
    assert (diagnostic.status, diagnostic.reason_code) == ("skipped", "missing_research_record")
    assert not [r for r in caplog.records if r.levelno >= logging.ERROR]


def test_negative_proof_quality_is_successful_execution(tmp_path):
    runner, result, _ = make_runner(tmp_path)
    review = replace(pipeline.review_research_proof(result.research_record), ok=False)
    with patch.object(pipeline, "review_research_proof", return_value=review):
        output = runner.run()
    assert output.audit_diagnostics[0].status == "ok"
    assert review.ok is False


@pytest.mark.parametrize("reason,status", [("missing_ledger", "skipped"), ("ledger_unavailable", "error")])
def test_ledger_snapshot_reason_survives_final_review(tmp_path, reason, status):
    ledger = EvidenceLedgerStore(tmp_path / "ledger")
    runner, _, _ = make_runner(tmp_path, accepted=False, ledger=ledger)
    with patch.object(ledger, "load", return_value=EvidenceLedgerSnapshot(False, tmp_path, reason_code=reason)):
        output = runner.run()
    rows = [d for d in output.audit_diagnostics if d.stage == "ledger_read"]
    assert len(rows) == 1
    assert (rows[0].status, rows[0].reason_code, rows[0].phase) == (status, reason, "final")
    reviews = [args[0] for name, args, _ in runner.context.trace.calls if name == "record_proof_review"]
    assert reviews[0].ledger_record_verified is False
    assert [d.status for d in output.audit_diagnostics if d.stage == "proof_review"] == ["ok", "ok"]


def test_ledger_exception_and_unconfigured_are_distinct(tmp_path, caplog):
    ledger = EvidenceLedgerStore(tmp_path / "ledger")
    runner, _, _ = make_runner(tmp_path, accepted=False, ledger=ledger)
    with patch.object(ledger, "load", side_effect=OSError("unreadable")):
        output = runner.run()
    row = next(d for d in output.audit_diagnostics if d.stage == "ledger_read")
    assert (row.status, row.reason_code, row.error_type) == ("error", "ledger_read_failed", "OSError")
    assert any(r.exc_info for r in caplog.records)
    runner.evidence_ledgers = None
    missing = runner._load_ledger(phase="final")
    assert (missing.diagnostic.status, missing.diagnostic.reason_code) == ("skipped", "ledger_not_configured")


def test_real_ledger_missing_corrupt_and_valid_states(tmp_path):
    ledger = EvidenceLedgerStore(tmp_path / "ledger")
    runner, _, _ = make_runner(tmp_path, accepted=False, ledger=ledger)
    missing = runner._load_ledger(phase="final")
    assert (missing.diagnostic.status, missing.diagnostic.reason_code) == ("skipped", "missing_ledger")
    path = ledger.path_for("s", "")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{broken", encoding="utf-8")
    corrupt = runner._load_ledger(phase="final")
    assert (corrupt.diagnostic.status, corrupt.diagnostic.reason_code) == ("error", "ledger_unavailable")
    # Existing append behavior rotates the corrupt active ledger and writes a valid one.
    output = runner.run()
    row = next(d for d in output.audit_diagnostics if d.stage == "ledger_read")
    assert row.status == "ok"
    review = next(args[0] for name, args, _ in runner.context.trace.calls if name == "record_proof_review")
    assert review.ledger_record_verified


def test_accepted_audit_does_not_load_ledger(tmp_path):
    ledger = EvidenceLedgerStore(tmp_path / "ledger")
    runner, _, _ = make_runner(tmp_path, ledger=ledger)
    with patch.object(ledger, "load") as load, patch.object(ledger, "append_accepted_report"):
        output = runner.run()
    load.assert_not_called()
    assert not [d for d in output.audit_diagnostics if d.stage == "ledger_read"]


def test_diagnostics_survive_real_trace_and_operation_payload(tmp_path):
    store = RunTraceStore(tmp_path)
    recorder = store.open(run_id="r", session_id="s", project=None, mode_initial="research", provider_initial="p")
    runner, result, _ = make_runner(tmp_path, trace=RunTraceResearchSink(recorder))
    with patch.object(pipeline, "review_research_proof", side_effect=ValueError("private")):
        output = runner.run()
    saved = json.loads(store.path_for("s", "r").read_text(encoding="utf-8"))
    expected = output.to_payload()["audit_diagnostics"]
    assert saved["research_pipeline_runs"][-1]["audit_diagnostics"] == expected
    assert research_payload(result, pipeline_result=output)["audit_diagnostics"] == expected


def test_real_trace_audit_write_failure_is_an_error(tmp_path):
    store = RunTraceStore(tmp_path)
    recorder = store.open(run_id="r", session_id="s", project=None, mode_initial="research", provider_initial="p")
    runner, _, _ = make_runner(tmp_path, trace=RunTraceResearchSink(recorder))
    with patch.object(recorder, "record_research_source_trust", side_effect=OSError("trace unavailable")):
        output = runner.run()
    row = next(d for d in output.audit_diagnostics if d.stage == "source_trust")
    assert (row.status, row.reason_code, row.error_type) == (
        "error", "source_trust_projection_failed", "OSError",
    )


def test_diagnostics_are_per_run_and_legacy_phases_survive_recovery(tmp_path):
    runner, result, _ = make_runner(tmp_path, accepted=False)
    real_review = pipeline.review_research_proof(result.research_record)
    with patch.object(pipeline, "review_research_proof", side_effect=[ValueError("initial"), real_review]):
        first = runner.run()
    rows = [d for d in first.audit_diagnostics if d.stage == "proof_review"]
    assert [(d.phase, d.status) for d in rows] == [("initial", "error"), ("final", "ok")]
    second = runner.run()
    assert not [d for d in second.audit_diagnostics if d.status == "error"]
    assert first.audit_diagnostics != second.audit_diagnostics


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


@pytest.mark.parametrize("stage,target,skips", [
    ("snapshot", "snapshot_from_research_record", {"findings", "planner_gaps", "brief"}),
    ("findings", "findings_from_proof_review", {"planner_gaps", "brief"}),
    ("planner_gaps", "planner_gaps_from_findings", set()),
    ("source_trust", "project_source_set", set()),
    ("brief", "project_research_brief", set()),
])
def test_projection_failures_only_skip_dependents(tmp_path, caplog, stage, target, skips):
    runner, result, search = make_runner(tmp_path)
    with patch.object(pipeline, target, side_effect=RuntimeError("projection broke")) as project:
        output = runner.run()
    project.assert_called_once()
    assert output.final_result is result and search.closed
    rows = {d.stage: d for d in output.audit_diagnostics}
    assert (rows[stage].status, rows[stage].reason_code, rows[stage].error_type) == (
        "error", f"{stage}_projection_failed", "RuntimeError",
    )
    for neighbor in {"snapshot", "findings", "planner_gaps", "source_trust", "brief"} - {stage}:
        assert rows[neighbor].status == ("skipped" if neighbor in skips else "ok")
    assert any(r.exc_info for r in caplog.records)


def test_empty_findings_are_success_and_allow_brief(tmp_path):
    runner, _, _ = make_runner(tmp_path)
    with (
        patch.object(pipeline, "findings_from_proof_review", return_value=()),
        patch.object(pipeline, "planner_gaps_from_findings", wraps=pipeline.planner_gaps_from_findings) as gaps,
        patch.object(pipeline, "project_research_brief", wraps=pipeline.project_research_brief) as brief,
    ):
        output = runner.run()
    rows = {d.stage: d for d in output.audit_diagnostics}
    assert all(rows[stage].status == "ok" for stage in ("findings", "planner_gaps", "brief"))
    gaps.assert_called_once_with(())
    assert brief.call_args.kwargs["findings"] == ()


@pytest.mark.parametrize("stage,target,skips", [
    ("snapshot", "snapshot_from_research_record", {"findings", "planner_gaps", "brief"}),
    ("brief", "project_research_brief", set()),
])
def test_unavailable_projection_has_skip_diagnostic(tmp_path, stage, target, skips):
    runner, _, _ = make_runner(tmp_path)
    with patch.object(pipeline, target, return_value=None):
        output = runner.run()
    rows = {d.stage: d for d in output.audit_diagnostics}
    assert (rows[stage].status, rows[stage].reason_code) == ("skipped", f"{stage}_unavailable")
    assert all(rows[name].status == "skipped" for name in skips)
    assert rows["source_trust"].status == "ok"


def test_source_trust_runs_without_proof_review(tmp_path):
    runner, _, _ = make_runner(tmp_path)
    with (
        patch.object(pipeline, "review_research_proof", side_effect=ValueError("no review")),
        patch.object(pipeline, "snapshot_from_research_record") as snapshot,
        patch.object(pipeline, "project_source_set", wraps=pipeline.project_source_set) as trust,
    ):
        output = runner.run()
    snapshot.assert_not_called()
    trust.assert_called_once()
    rows = {d.stage: d for d in output.audit_diagnostics}
    assert rows["source_trust"].status == "ok"
    assert (rows["snapshot"].status, rows["snapshot"].reason_code) == ("skipped", "proof_review_unavailable")


def test_trace_projection_error_is_isolated(tmp_path):
    trace = _TraceRecorder()
    runner, _, _ = make_runner(tmp_path, trace=trace)
    with patch.object(trace, "record_review_findings", side_effect=ValueError("trace conversion failed")):
        output = runner.run()
    rows = {d.stage: d for d in output.audit_diagnostics}
    assert rows["findings"].status == "error"
    assert rows["source_trust"].status == "ok"
    assert rows["brief"].reason_code == "findings_unavailable"


def test_missing_record_skips_all_projections(tmp_path):
    runner, _, _ = make_runner(tmp_path, record=False)
    with patch.object(pipeline, "project_source_set") as trust:
        output = runner.run()
    trust.assert_not_called()
    assert all(d.status == "skipped" and d.reason_code == "missing_research_record" for d in output.audit_diagnostics)
    assert {d.stage for d in output.audit_diagnostics} == {
        "proof_review", "snapshot", "findings", "planner_gaps", "source_trust", "brief",
    }


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
