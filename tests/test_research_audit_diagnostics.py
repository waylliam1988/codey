"""Audit execution failures remain visible without changing publication gates."""

import json
import logging
from dataclasses import replace
from unittest.mock import patch

import pytest

from codey.operations.research_flow import research_payload
from codey.research import pipeline
from codey.research.audit_result import MAX_AUDIT_DIAGNOSTICS
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
    assert project_research_pipeline_result({})["audit_diagnostics"] == []
