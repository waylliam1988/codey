from __future__ import annotations

import tempfile
from dataclasses import replace
from pathlib import Path

from codey.knowledge.changes import KnowledgeChanges
from codey.knowledge.store import KnowledgeStore
from codey.research.context import ResearchContext, ResearchPipelineConfig, RunTraceResearchSink
from codey.research.evidence_ledger import EvidenceLedgerStore
from codey.research.ledger import ResearchLedger
from codey.research.object_model import build_research_record
from codey.research.pipeline import ResearchIterationRun, ResearchPipeline, _selects_candidate
from codey.research.plan_executor import PlanExecutionResult
from codey.research.proof_quality import CoverageGap, ResearchProofReview
from codey.research.query_planner import QueryCandidate, ResearchPlan
from codey.research.report_quality import review_report_quality
from codey.research.runner import ResearchRunResult
from codey.research.tools import ResearchTools


class _TraceRecorder:
    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple, dict]] = []

    def record_permission_profile(self, *args, **kwargs) -> None:
        self.calls.append(("record_permission_profile", args, kwargs))

    def record_research_notes(self, *args, **kwargs) -> None:
        self.calls.append(("record_research_notes", args, kwargs))

    def record_research_sources(self, *args, **kwargs) -> None:
        self.calls.append(("record_research_sources", args, kwargs))

    def record_research_record_summary(self, *args, **kwargs) -> None:
        self.calls.append(("record_research_record_summary", args, kwargs))

    def record_research_plan(self, *args, **kwargs) -> None:
        self.calls.append(("record_research_plan", args, kwargs))

    def record_research_pipeline_result(self, *args, **kwargs) -> None:
        self.calls.append(("record_research_pipeline_result", args, kwargs))

    def record_research_proof_review(self, *args, **kwargs) -> None:
        self.calls.append(("record_research_proof_review", args, kwargs))

    def record_evidence_ledger_write(self, *args, **kwargs) -> None:
        self.calls.append(("record_evidence_ledger_write", args, kwargs))

    def flush(self) -> None:
        self.calls.append(("flush", (), {}))


class _PipelineSearch:
    def __init__(self) -> None:
        self.closed = False

    def search(self, *_args, **_kwargs) -> list[dict]:
        return []

    def fetch(self, url: str) -> dict:
        return {
            "url": url,
            "title": "Pipeline source",
            "text": "Pipeline source text says the fact.",
            "truncated": False,
        }

    def close(self) -> None:
        self.closed = True


def _record(
    *,
    question: str,
    synthesis_id: str,
    project: Path,
    url: str = "https://example.com/pipeline",
) -> object:
    ledger = ResearchLedger()
    summary = (
        "## 结论\n"
        "- Pipeline answer depends on the opened source. [1]\n\n"
        "## 关键证据\n"
        "- [1] Pipeline source text says the fact.\n\n"
        "## 反证与限制\n"
        "- 未找到强反证；需要持续追踪新数据。\n\n"
        "## 来源质量\n"
        "- [1] secondary · web · fresh · example.com\n\n"
        "## 搜索覆盖\n"
        "- query: pipeline question\n"
        "- opened: Pipeline source\n"
        "- skipped: none representative\n\n"
        "## 来源\n"
        f"[1] Pipeline source - {url}"
    )
    ledger.record_search("pipeline question", [{
        "title": "Pipeline source",
        "url": url,
        "snippet": "Pipeline source text.",
    }])
    ledger.record_open(
        requested_url=url,
        final_url=url,
        title="Pipeline source",
        text="Pipeline source text says the fact.",
    )
    prepared = ledger.prepare_evidence_items(
        [{
            "claim": "Pipeline answer depends on the opened source.",
            "source_url": url,
            "excerpt": "Pipeline source text says the fact.",
            "stance": "supports",
        }],
        fallback_sources=[url],
        fallback_claim="Pipeline answer depends on the opened source.",
        fallback_body="Pipeline source text says the fact.",
        note_type="fact",
    )
    assert not prepared.error
    ledger.add_evidence_items(list(prepared.items), note_id="note-1")
    review = review_report_quality(
        summary,
        ledger=ledger,
        opened_sources=ledger.final_url_set(),
        search_result_urls={url},
    )
    assert review.ok
    return build_research_record(
        question=question,
        summary=summary,
        ledger=ledger,
        review=review,
        run_id="run-" + synthesis_id,
        session_id="session-pipeline",
        project=project,
        synthesis_id=synthesis_id,
        stop_reason="done",
    )


def _result(
    *,
    question: str,
    summary: str,
    stop_reason: str,
    synthesis_id: str,
    record,
    tools: ResearchTools,
    notes: list[str] | None = None,
) -> ResearchIterationRun:
    return ResearchIterationRun(
        result=ResearchRunResult(
            question=question,
            summary=summary,
            stop_reason=stop_reason,
            turns=1,
            notes_created=notes or [],
            opened_sources=[{
                "requested_url": "https://example.com/pipeline",
                "final_url": "https://example.com/pipeline",
                "title": "Pipeline source",
            }],
            synthesis_id=synthesis_id,
            research_record=record,
        ),
        tools=tools,
    )


def _review(
    *,
    record_id: str,
    record_digest: str,
    ok: bool,
    answer_status: str,
    score: float,
    missing: tuple[str, ...] = (),
) -> ResearchProofReview:
    return ResearchProofReview(
        ok=ok,
        answers_question=True,
        answer_status=answer_status,
        answer_coverage_score=score,
        citation_present=True,
        citation_locator_verified=True,
        support_relation_verified=True,
        counterevidence_checked=True,
        ledger_record_verified=False,
        question_digest="sha256:" + "1" * 64,
        coverage_gaps=(CoverageGap(reason_code="coverage_gap"),) if not ok else (),
        followup_questions=(),
        query_rewrite_candidates=(),
        source_trust_warnings=(),
        overclaim_warnings=(),
        stale_warnings=(),
        missing_evidence=missing,
        proof_ref="research_proof:" + answer_status,
        record_id=record_id,
        record_digest=record_digest,
    )


def test_pipeline_skips_followup_when_proof_is_ok_and_appends_ledger_once() -> None:
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
        root = Path(td)
        project = root / "project"
        project.mkdir()
        state_root = root / "state"
        store = KnowledgeStore(root / "knowledge")
        changes = KnowledgeChanges(root=store.root)
        search = _PipelineSearch()
        tools = ResearchTools(search=search, store=store, changes=changes, session_id="session-pipeline", project="project-pipeline")
        trace = _TraceRecorder()
        record = _record(question="Pipeline question", synthesis_id="initial", project=project)
        result = _result(
            question="Pipeline question",
            summary="initial summary",
            stop_reason="done",
            synthesis_id="initial",
            record=record,
            tools=tools,
        )
        evidence_ledgers = EvidenceLedgerStore(state_root)
        events: list[tuple[str, object]] = []

        def run_iteration(**_kwargs):
            return result

        def fake_review(record, *, question: str = "", evidence_ledger=None, require_ledger_record: bool = False):
            del question, evidence_ledger
            return replace(
                _review(
                    record_id=getattr(record, "record_id", ""),
                    record_digest=getattr(record, "record_digest", ""),
                    ok=True,
                    answer_status="answered",
                    score=1.0,
                ),
                ledger_record_verified=require_ledger_record,
            )

        def fake_plan(review, *, question: str = "", max_queries: int, max_sources: int):
            del question
            return ResearchPlan(
                plan_ref="research_plan:noop",
                proof_ref=getattr(review, "proof_ref", ""),
                query_candidates=(),
                reason_codes=("proof_ok_no_required_followup",),
                max_queries=max_queries,
                max_sources=max_sources,
            )

        context = ResearchContext(
            question="Pipeline question",
            session_id="session-pipeline",
            run_id="run-pipeline",
            project="project-pipeline",
            proof_question="Pipeline question",
            max_turns=4,
            should_stop=lambda: False,
            trace=RunTraceResearchSink(trace),
        )

        from codey.research import pipeline as pipeline_module

        original_review = pipeline_module.review_research_proof
        original_plan = pipeline_module.build_research_plan
        original_execute = pipeline_module.PlanExecutor.execute
        try:
            pipeline_module.review_research_proof = fake_review
            pipeline_module.build_research_plan = fake_plan
            pipeline_module.PlanExecutor.execute = lambda self, plan, tools: (_ for _ in ()).throw(
                AssertionError("follow-up search should not run when proof is already ok")
            )
            pipeline = ResearchPipeline(
                context=context,
                run_iteration=run_iteration,
                search_factory=lambda: search,
                evidence_ledgers=evidence_ledgers,
                config=ResearchPipelineConfig(enabled=True, max_followup_rounds=1),
                ledger_event_sink=lambda item: events.append(("ledger", item)),
                research_changes_sink=lambda run_id, snapshot: events.append((run_id, snapshot)),
            )
            output = pipeline.run()
        finally:
            pipeline_module.review_research_proof = original_review
            pipeline_module.build_research_plan = original_plan
            pipeline_module.PlanExecutor.execute = original_execute

        assert output.final_result is result.result
        assert output.followup_applied is False
        assert output.followup_rounds == 0
        assert search.closed is True
        assert len([item for item in events if item[0] == "ledger"]) == 1
        assert any(item[0] == "run-pipeline" for item in events)
        snapshot = evidence_ledgers.load(session_id="session-pipeline", project="project-pipeline")
        assert snapshot.available is True
        assert len(snapshot.payload["records"]) == 1
        assert any(name == "record_evidence_ledger_write" for name, *_ in trace.calls)
        assert any(name == "record_research_plan" for name, *_ in trace.calls)
        assert any(name == "record_research_pipeline_result" for name, *_ in trace.calls)


def test_pipeline_prefers_better_followup_but_rejects_unsupported_regression() -> None:
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
        root = Path(td)
        project = root / "project"
        project.mkdir()
        state_root = root / "state"
        store = KnowledgeStore(root / "knowledge")
        changes = KnowledgeChanges(root=store.root)
        search = _PipelineSearch()
        tools = ResearchTools(search=search, store=store, changes=changes, session_id="session-pipeline", project="project-pipeline")
        trace = _TraceRecorder()
        initial_record = _record(question="Pipeline question", synthesis_id="initial", project=project)
        candidate_record = _record(question="Pipeline question", synthesis_id="candidate", project=project)
        initial = _result(
            question="Pipeline question",
            summary="initial summary",
            stop_reason="max_turns",
            synthesis_id="initial",
            record=initial_record,
            tools=tools,
        )
        candidate = _result(
            question="Pipeline question",
            summary="candidate summary",
            stop_reason="done",
            synthesis_id="candidate",
            record=candidate_record,
            tools=tools,
        )
        evidence_ledgers = EvidenceLedgerStore(state_root)
        run_calls: list[dict[str, object]] = []
        plan_calls: list[str] = []
        ledger_events: list[str] = []

        def run_iteration(**kwargs):
            run_calls.append(kwargs)
            return initial if len(run_calls) == 1 else candidate

        def fake_review(record, *, question: str = "", evidence_ledger=None, require_ledger_record: bool = False):
            del question, evidence_ledger
            if getattr(record, "synthesis_id", "") == "initial":
                base = _review(
                    record_id=getattr(record, "record_id", ""),
                    record_digest=getattr(record, "record_digest", ""),
                    ok=False,
                    answer_status="insufficient_evidence",
                    score=0.35,
                    missing=("answer_coverage_gap",),
                )
            else:
                base = _review(
                    record_id=getattr(record, "record_id", ""),
                    record_digest=getattr(record, "record_digest", ""),
                    ok=False,
                    answer_status="partial",
                    score=0.75,
                    missing=("partial_answer",),
                )
            return replace(base, ledger_record_verified=require_ledger_record)

        def fake_plan(review, *, question: str = "", max_queries: int, max_sources: int):
            del question
            plan_calls.append(getattr(review, "answer_status", ""))
            if getattr(review, "ok", False):
                return ResearchPlan(
                    plan_ref="research_plan:noop",
                    proof_ref=getattr(review, "proof_ref", ""),
                    query_candidates=(),
                    reason_codes=("proof_ok_no_required_followup",),
                    max_queries=max_queries,
                    max_sources=max_sources,
                )
            return ResearchPlan(
                plan_ref="research_plan:" + getattr(review, "answer_status", "none"),
                proof_ref=getattr(review, "proof_ref", ""),
                query_candidates=(
                    QueryCandidate("research_query:" + "1" * 16, "follow-up query"),
                ),
                reason_codes=("proof_gap",),
                max_queries=max_queries,
                max_sources=max_sources,
            )

        followup_material = PlanExecutionResult(
            queries_executed=("follow-up query",),
            opened_sources=({
                "requested_url": "https://example.com/followup",
                "final_url": "https://example.com/followup",
                "title": "Followup source",
            },),
            previews=("query: follow-up query\nFollowup source | https://example.com/followup\nFollow-up body",),
            skipped_count=0,
            stop_reason="opened_sources",
        )

        def fake_execute(self, plan, tools):
            del plan, tools
            return followup_material

        context = ResearchContext(
            question="Pipeline question",
            session_id="session-pipeline",
            run_id="run-pipeline",
            project="project-pipeline",
            proof_question="Pipeline question",
            max_turns=4,
            should_stop=lambda: False,
            trace=RunTraceResearchSink(trace),
        )

        from codey.research import pipeline as pipeline_module

        original_review = pipeline_module.review_research_proof
        original_plan = pipeline_module.build_research_plan
        original_execute = pipeline_module.PlanExecutor.execute
        try:
            pipeline_module.review_research_proof = fake_review
            pipeline_module.build_research_plan = fake_plan
            pipeline_module.PlanExecutor.execute = fake_execute
            pipeline = ResearchPipeline(
                context=context,
                run_iteration=run_iteration,
                search_factory=lambda: search,
                evidence_ledgers=evidence_ledgers,
                config=ResearchPipelineConfig(enabled=True, max_followup_rounds=1),
                ledger_event_sink=lambda item: ledger_events.append(item.reason_code),
                research_changes_sink=lambda run_id, snapshot: ledger_events.append(run_id if snapshot is changes else "unexpected"),
            )
            output = pipeline.run()
        finally:
            pipeline_module.review_research_proof = original_review
            pipeline_module.build_research_plan = original_plan
            pipeline_module.PlanExecutor.execute = original_execute

        assert output.followup_applied is True
        assert output.followup_rounds == 1
        assert output.final_result is candidate.result
        assert output.planner_stop_reason == "max_followup_rounds"
        assert search.closed is True
        assert len(run_calls) == 2
        assert "queries_executed:" in str(run_calls[1]["iteration_context"])
        assert "Followup source | https://example.com/followup" in str(run_calls[1]["iteration_context"])
        assert any(name == "record_evidence_ledger_write" for name, *_ in trace.calls)
        assert any(name == "record_research_record_summary" for name, *_ in trace.calls)
        snapshot = evidence_ledgers.load(session_id="session-pipeline", project="project-pipeline")
        assert snapshot.available is True
        assert len(snapshot.payload["records"]) == 1
        assert "run-pipeline" in ledger_events
        assert "insufficient_evidence" in plan_calls


def test_pipeline_keeps_initial_result_when_followup_iteration_raises() -> None:
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
        root = Path(td)
        project = root / "project"
        project.mkdir()
        state_root = root / "state"
        store = KnowledgeStore(root / "knowledge")
        changes = KnowledgeChanges(root=store.root)
        search = _PipelineSearch()
        tools = ResearchTools(search=search, store=store, changes=changes, session_id="session-pipeline", project="project-pipeline")
        trace = _TraceRecorder()
        initial_record = _record(question="Pipeline question", synthesis_id="initial", project=project)
        initial = _result(
            question="Pipeline question",
            summary="initial summary",
            stop_reason="done",
            synthesis_id="initial",
            record=initial_record,
            tools=tools,
        )
        evidence_ledgers = EvidenceLedgerStore(state_root)
        run_calls: list[dict[str, object]] = []

        def run_iteration(**kwargs):
            run_calls.append(kwargs)
            if len(run_calls) == 1:
                return initial
            raise RuntimeError("follow-up synthesis failed")

        def fake_review(record, *, question: str = "", evidence_ledger=None, require_ledger_record: bool = False):
            del question, evidence_ledger
            return replace(
                _review(
                    record_id=getattr(record, "record_id", ""),
                    record_digest=getattr(record, "record_digest", ""),
                    ok=False,
                    answer_status="insufficient_evidence",
                    score=0.35,
                    missing=("answer_coverage_gap",),
                ),
                ledger_record_verified=require_ledger_record,
            )

        def fake_plan(review, *, question: str = "", max_queries: int, max_sources: int):
            del question
            return ResearchPlan(
                plan_ref="research_plan:" + getattr(review, "answer_status", "none"),
                proof_ref=getattr(review, "proof_ref", ""),
                query_candidates=(
                    QueryCandidate("research_query:" + "2" * 16, "follow-up query"),
                ),
                reason_codes=("proof_gap",),
                max_queries=max_queries,
                max_sources=max_sources,
            )

        followup_material = PlanExecutionResult(
            queries_executed=("follow-up query",),
            opened_sources=({
                "requested_url": "https://example.com/followup",
                "final_url": "https://example.com/followup",
                "title": "Followup source",
            },),
            previews=("query: follow-up query\nFollowup source | https://example.com/followup\nFollow-up body",),
            skipped_count=0,
            stop_reason="opened_sources",
        )

        context = ResearchContext(
            question="Pipeline question",
            session_id="session-pipeline",
            run_id="run-pipeline",
            project="project-pipeline",
            proof_question="Pipeline question",
            max_turns=4,
            should_stop=lambda: False,
            trace=RunTraceResearchSink(trace),
        )

        from codey.research import pipeline as pipeline_module

        original_review = pipeline_module.review_research_proof
        original_plan = pipeline_module.build_research_plan
        original_execute = pipeline_module.PlanExecutor.execute
        try:
            pipeline_module.review_research_proof = fake_review
            pipeline_module.build_research_plan = fake_plan
            pipeline_module.PlanExecutor.execute = lambda self, plan, tools: followup_material
            pipeline = ResearchPipeline(
                context=context,
                run_iteration=run_iteration,
                search_factory=lambda: search,
                evidence_ledgers=evidence_ledgers,
                config=ResearchPipelineConfig(enabled=True, max_followup_rounds=1),
            )
            output = pipeline.run()
        finally:
            pipeline_module.review_research_proof = original_review
            pipeline_module.build_research_plan = original_plan
            pipeline_module.PlanExecutor.execute = original_execute

        assert output.final_result is initial.result
        assert output.followup_applied is False
        assert output.followup_rounds == 0
        assert output.planner_stop_reason == "followup_iteration_error"
        assert len(run_calls) == 2
        snapshot = evidence_ledgers.load(session_id="session-pipeline", project="project-pipeline")
        assert snapshot.available is True
        assert len(snapshot.payload["records"]) == 1
        assert snapshot.payload["records"][0]["synthesis_id"] == "initial"
        assert search.closed is True


def test_pipeline_selection_rejects_unsupported_claim_regression() -> None:
    current = ResearchRunResult("question", "initial", "done", 1)
    candidate = ResearchRunResult("question", "candidate", "done", 1)
    current_review = _review(
        record_id="research_record:" + "1" * 16,
        record_digest="sha256:" + "1" * 64,
        ok=False,
        answer_status="insufficient_evidence",
        score=0.35,
        missing=("answer_coverage_gap",),
    )
    candidate_review = _review(
        record_id="research_record:" + "2" * 16,
        record_digest="sha256:" + "2" * 64,
        ok=False,
        answer_status="answered",
        score=1.0,
        missing=("unsupported_claims",),
    )

    assert _selects_candidate(candidate, candidate_review, current, current_review) is False
