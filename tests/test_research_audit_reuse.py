"""Audit reuse preserves publication diagnostics and ledger-dependent checks."""

from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from codey.research import pipeline
from codey.research.context import ResearchContext
from codey.research.evidence_ledger import EvidenceLedgerStore
from codey.research.run_result import ResearchRunResult
from tests.test_research_deterministic_completion import _run
from tests.test_research_pipeline import _allow_policy, _PipelineSearch, _record, _TraceRecorder
from tests.test_research_whole_kernel import run_whole


@pytest.mark.usefixtures("public_source_dns")
@pytest.mark.parametrize("whole", [False, True])
@pytest.mark.parametrize("native", [False, True])
def test_accepted_publication_reuses_audit_and_keeps_diagnostics(tmp_path, whole, native):
    trace = _TraceRecorder()
    ledger = EvidenceLedgerStore(tmp_path / "ledger")
    events = []
    original_init = pipeline.ResearchPipeline.__init__

    def init(self, **kwargs):
        kwargs["context"] = replace(kwargs["context"], trace=trace)
        kwargs["evidence_ledgers"] = ledger
        kwargs["ledger_event_sink"] = events.append
        original_init(self, **kwargs)

    with (
        patch.object(pipeline.ResearchPipeline, "__init__", init),
        patch.object(pipeline, "review_research_proof", wraps=pipeline.review_research_proof) as review,
        patch.object(pipeline, "build_research_plan", wraps=pipeline.build_research_plan) as plan,
    ):
        result = run_whole(tmp_path, native=native) if whole else _run(tmp_path, native=native)

    assert result.stop_reason == "done", result.summary
    assert result.synthesis_id and result.completion_proof.satisfied
    assert review.call_count == plan.call_count == 1
    assert review.call_args.kwargs["require_ledger_record"] is False
    reviews = [args[0] for name, args, _ in trace.calls if name == "record_proof_review"]
    plans = [args[0] for name, args, _ in trace.calls if name == "record_plan"]
    # Recomputing from the final record would give the identical diagnostic.
    expected = pipeline.review_research_proof(*review.call_args.args, **review.call_args.kwargs)
    assert reviews == [expected]
    assert len(plans) == 2 and plans[0] is plans[1]
    assert plans[1] == pipeline.build_research_plan(*plan.call_args.args, **plan.call_args.kwargs)
    writes = [args[0] for name, args, _ in trace.calls if name == "record_evidence_ledger_write"]
    assert writes == events and len(writes) == 1
    assert writes[0].ok and not writes[0].skipped, writes[0].reason_code
    saved = ledger.load(session_id="s", project="")
    assert saved.available
    if whole:
        assert not saved.payload["claims"]  # Body citations never manufacture verified claims.
        assert {row["stance"] for row in saved.payload["evidence"].values()} == {"cited_unreviewed"}
    names = [name for name, _, _ in trace.calls]
    assert "record_research_source_trust" in names
    assert "record_research_brief_projection" in names
    assert names.index("record_result") < names.index("record_proof_review")
    assert names.index("record_proof_review") < names.index("record_research_brief_projection")
    assert names[-1] == "record_pipeline_result"


@pytest.mark.parametrize("review_failure", [False, True])
def test_accepted_publication_does_not_retry_an_unavailable_audit(tmp_path, review_failure):
    trace = _TraceRecorder()
    search = _PipelineSearch()
    record = _record(question="Pipeline question", synthesis_id="answer", project=tmp_path)
    result = ResearchRunResult(
        question="Pipeline question", summary="Saved answer", stop_reason="done", turns=1,
        research_record=record, accepted_report=object(),
    )
    runner = pipeline.ResearchPipeline(
        context=ResearchContext(question=result.question, session_id="s", run_id="r", trace=trace),
        run_iteration=lambda **_: pipeline.ResearchIterationRun(result),
        search_factory=lambda: search, policy=_allow_policy(),
    )
    with (
        patch.object(
            pipeline, "review_research_proof", return_value=None,
            side_effect=ValueError("audit unavailable") if review_failure else None,
        ) as review,
        patch.object(pipeline, "build_research_plan", wraps=pipeline.build_research_plan) as plan,
    ):
        output = runner.run()
    assert output.final_result is result
    assert search.closed
    assert review.call_count == plan.call_count == 1
    assert [(name, args) for name, args, _ in trace.calls if name == "record_proof_review"] == [
        ("record_proof_review", (None,)),
    ]
    plans = [args[0] for name, args, _ in trace.calls if name == "record_plan"]
    assert len(plans) == 2 and plans[0] is plans[1]


def test_accepted_ledger_write_failure_keeps_audit_and_reports_failure(tmp_path):
    trace = _TraceRecorder()
    ledger = EvidenceLedgerStore(tmp_path / "ledger")
    record = _record(question="Pipeline question", synthesis_id="answer", project=tmp_path)
    result = ResearchRunResult(
        question="Pipeline question", summary="Saved answer", stop_reason="done", turns=1,
        research_record=record, accepted_report=SimpleNamespace(to_payload=lambda: {}),
    )
    events = []
    runner = pipeline.ResearchPipeline(
        context=ResearchContext(question=result.question, session_id="s", run_id="r", trace=trace),
        run_iteration=lambda **_: pipeline.ResearchIterationRun(result),
        search_factory=_PipelineSearch, policy=_allow_policy(), evidence_ledgers=ledger,
        ledger_event_sink=events.append,
    )
    with (
        patch.object(ledger, "append_accepted_report", side_effect=OSError("disk unavailable")),
        patch.object(pipeline, "review_research_proof", wraps=pipeline.review_research_proof) as review,
        patch.object(pipeline, "build_research_plan", wraps=pipeline.build_research_plan) as plan,
    ):
        output = runner.run()
    assert output.final_result is result
    assert review.call_count == plan.call_count == 1
    assert len(events) == 1 and events[0].reason_code == "write_failed"
    writes = [args[0] for name, args, _ in trace.calls if name == "record_evidence_ledger_write"]
    assert writes == events
    assert next(args[0] for name, args, _ in trace.calls if name == "record_proof_review") is not None


@pytest.mark.parametrize("write_failure", [False, True])
def test_nonaccepted_record_is_reviewed_again_after_ledger_write(tmp_path, write_failure):
    trace = _TraceRecorder()
    ledger = EvidenceLedgerStore(tmp_path / "ledger")
    record = _record(question="Pipeline question", synthesis_id="answer", project=tmp_path)
    result = ResearchRunResult(
        question="Pipeline question", summary="Legacy result", stop_reason="done", turns=1,
        research_record=record,
    )
    runner = pipeline.ResearchPipeline(
        context=ResearchContext(question=result.question, session_id="s", run_id="r", trace=trace),
        run_iteration=lambda **_: pipeline.ResearchIterationRun(result),
        search_factory=_PipelineSearch, policy=_allow_policy(), evidence_ledgers=ledger,
    )
    original_append = ledger.append_record
    with (
        patch.object(
            ledger, "append_record", wraps=original_append,
            side_effect=OSError("disk unavailable") if write_failure else None,
        ),
        patch.object(pipeline, "review_research_proof", wraps=pipeline.review_research_proof) as review,
        patch.object(pipeline, "build_research_plan", wraps=pipeline.build_research_plan) as plan,
    ):
        output = runner.run()
    assert output.final_result is result
    assert review.call_count == plan.call_count == 2
    assert [call.kwargs["require_ledger_record"] for call in review.call_args_list] == [False, True]
    final_review = next(args[0] for name, args, _ in trace.calls if name == "record_proof_review")
    assert final_review.ledger_record_verified is not write_failure
    write = next(args[0] for name, args, _ in trace.calls if name == "record_evidence_ledger_write")
    assert write.skipped is write_failure
    if write_failure:
        assert write.reason_code == "write_failed"
    names = [name for name, _, _ in trace.calls]
    assert names.index("record_evidence_ledger_write") < names.index("record_proof_review")
