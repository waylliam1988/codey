from __future__ import annotations

import tempfile
from dataclasses import replace
from pathlib import Path

import pytest

from codey.knowledge.changes import KnowledgeChanges
from codey.knowledge.store import KnowledgeStore
from codey.research.context import ResearchContext, ResearchPipelineConfig, RunTraceResearchSink
from codey.research.evidence_followup import EvidenceFollowupResult
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


def test_pipeline_reports_missing_proof_review_without_followup() -> None:
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
        root = Path(td)
        store = KnowledgeStore(root / "knowledge")
        changes = KnowledgeChanges(root=store.root)
        search = _PipelineSearch()
        tools = ResearchTools(
            search=search,
            store=store,
            changes=changes,
            session_id="session-pipeline",
            project="project-pipeline",
        )
        trace = _TraceRecorder()
        result = ResearchRunResult(
            question="Pipeline question",
            summary="initial summary",
            stop_reason="done",
            turns=1,
        )

        def run_iteration(**_kwargs):
            return ResearchIterationRun(result=result, tools=tools)

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

        original_execute = pipeline_module.PlanExecutor.execute
        try:
            pipeline_module.PlanExecutor.execute = lambda self, plan, tools: (_ for _ in ()).throw(
                AssertionError("follow-up search should not run without a proof review")
            )
            pipeline = ResearchPipeline(
                context=context,
                run_iteration=run_iteration,
                search_factory=lambda: search,
                config=ResearchPipelineConfig(enabled=True, max_followup_rounds=1),
            )
            output = pipeline.run()
        finally:
            pipeline_module.PlanExecutor.execute = original_execute

        assert output.final_result is result
        assert output.followup_applied is False
        assert output.followup_rounds == 0
        assert output.planner_stop_reason == "proof_review_missing"
        assert search.closed is True


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
            fresh_source_urls=("https://example.com/followup",),
            previews=("query: follow-up query\nFollowup source | https://example.com/followup\nFollow-up body",),
            skipped_count=0,
            stop_reason="opened_sources",
        )

        def fake_execute(self, plan, tools):
            del plan, tools
            return followup_material

        from codey.research.evidence_followup import EvidenceFollowupResult

        def fake_followup_runner(*, tools, plan, material, question, initial_summary="", max_context_chars=8000, should_stop=None):
            del tools, plan, material, question, initial_summary, max_context_chars, should_stop
            return EvidenceFollowupResult(
                ok=True,
                new_evidence_count=1,
                written_note_ids=("note:followup",),
                new_source_urls=("https://example.com/followup",),
                stop_reason="written",
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
        original_merge = pipeline_module.merge_evidence_patch
        try:
            pipeline_module.review_research_proof = fake_review
            pipeline_module.build_research_plan = fake_plan
            pipeline_module.PlanExecutor.execute = fake_execute
            pipeline_module.merge_evidence_patch = lambda initial, tools, material: candidate.result
            pipeline = ResearchPipeline(
                context=context,
                run_iteration=run_iteration,
                search_factory=lambda: search,
                evidence_followup_runner=fake_followup_runner,
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
            pipeline_module.merge_evidence_patch = original_merge

        assert output.followup_applied is True
        assert output.followup_rounds == 1
        assert output.final_result is candidate.result
        assert output.planner_stop_reason in {"evidence_merged", "max_followup_rounds"}
        assert output.fresh_source_count == 1
        assert output.new_evidence_count == 1
        assert search.closed is True
        assert len(run_calls) == 1
        assert any(name == "record_evidence_ledger_write" for name, *_ in trace.calls)
        assert any(name == "record_research_record_summary" for name, *_ in trace.calls)
        snapshot = evidence_ledgers.load(session_id="session-pipeline", project="project-pipeline")
        assert snapshot.available is True
        assert len(snapshot.payload["records"]) == 1
        assert "run-pipeline" in ledger_events
        assert "insufficient_evidence" in plan_calls


def test_pipeline_staging_isolates_rejected_followup_side_effects() -> None:
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
        root = Path(td)
        project = root / "project"
        project.mkdir()
        store = KnowledgeStore(root / "knowledge")
        changes = KnowledgeChanges(root=store.root)
        search = _PipelineSearch()
        initial_tools = ResearchTools(search=search, store=store, changes=changes, session_id="session-p", project="project-p")
        trace = _TraceRecorder()
        initial_record = _record(question="Pipeline question", synthesis_id="initial", project=project)
        initial = _result(
            question="Pipeline question",
            summary="initial summary",
            stop_reason="done",
            synthesis_id="initial",
            record=initial_record,
            tools=initial_tools,
        )

        def run_iteration(**_kwargs):
            return initial

        def fake_followup_runner(*, tools, **_kwargs):
            # Write to the passed-in (staged) tools ledger
            from codey.research.source_document import SourceDocument
            tools.ledger.record_open_document(SourceDocument.html(
                requested_url="https://example.com/rejected",
                final_url="https://example.com/rejected",
                title="Rejected Source",
                text="Staged text",
            ))
            tools.knowledge_write({
                "type": "fact",
                "title": "Rejected Claim",
                "body": "Staged body",
                "sources": ["https://example.com/rejected"],
                "evidence": [{
                    "source_url": "https://example.com/rejected",
                    "excerpt": "Staged text",
                    "claim": "Rejected Claim",
                }],
            })
            return EvidenceFollowupResult(
                ok=True,
                new_evidence_count=1,
                new_source_urls=("https://example.com/rejected",),
                stop_reason="written",
            )

        def fake_review(record, *, question: str = "", evidence_ledger=None, require_ledger_record: bool = False):
            del question, evidence_ledger
            return _review(
                record_id=getattr(record, "record_id", ""),
                record_digest=getattr(record, "record_digest", ""),
                ok=False,
                answer_status="insufficient_evidence",
                score=0.35,
                missing=("answer_coverage_gap",),
            )

        def fake_plan(review, *, max_queries: int, max_sources: int, **_kwargs):
            return ResearchPlan(
                plan_ref="research_plan:gap",
                query_candidates=(QueryCandidate("q:1", "gap query"),),
                reason_codes=("proof_gap",),
                max_queries=max_queries,
                max_sources=max_sources,
            )

        material = PlanExecutionResult(
            queries_executed=("gap query",),
            fresh_source_urls=("https://example.com/rejected",),
            stop_reason="opened_sources",
        )

        context = ResearchContext(
            question="Pipeline question",
            session_id="session-p",
            run_id="run-p",
            project="project-p",
            max_turns=4,
            trace=RunTraceResearchSink(trace),
        )

        from codey.research import pipeline as pipeline_module

        original_review = pipeline_module.review_research_proof
        original_plan = pipeline_module.build_research_plan
        original_execute = pipeline_module.PlanExecutor.execute
        original_selects = pipeline_module._selects_candidate
        try:
            pipeline_module.review_research_proof = fake_review
            pipeline_module.build_research_plan = fake_plan
            pipeline_module.PlanExecutor.execute = lambda self, plan, tools: material
            # Candidate is explicitly rejected
            pipeline_module._selects_candidate = lambda cand, cr, best, br: False
            pipeline = ResearchPipeline(
                context=context,
                run_iteration=run_iteration,
                search_factory=lambda: search,
                evidence_followup_runner=fake_followup_runner,
                config=ResearchPipelineConfig(enabled=True, max_followup_rounds=1),
            )
            output = pipeline.run()
        finally:
            pipeline_module.review_research_proof = original_review
            pipeline_module.build_research_plan = original_plan
            pipeline_module.PlanExecutor.execute = original_execute
            pipeline_module._selects_candidate = original_selects

        assert output.final_result is initial.result
        assert output.planner_stop_reason == "candidate_not_selected"
        # Initial tools state and store were NOT polluted by the rejected staging run
        assert len(initial_tools.ledger.evidence_items) == 0
        assert "https://example.com/rejected" not in initial_tools.sources_read
        assert len(initial_tools.created_ids) == 0
        # KnowledgeStore on disk contains zero written notes
        assert len(list(store.root.glob("**/*.md"))) == 0


def test_pipeline_staging_commits_accepted_followup_side_effects() -> None:
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
        root = Path(td)
        project = root / "project"
        project.mkdir()
        store = KnowledgeStore(root / "knowledge")
        changes = KnowledgeChanges(root=store.root)
        search = _PipelineSearch()
        initial_tools = ResearchTools(search=search, store=store, changes=changes, session_id="session-acc", project="project-acc")
        trace = _TraceRecorder()
        initial_record = _record(question="Pipeline question", synthesis_id="initial", project=project)
        initial = _result(
            question="Pipeline question",
            summary="initial summary",
            stop_reason="done",
            synthesis_id="initial",
            record=initial_record,
            tools=initial_tools,
        )

        def run_iteration(**_kwargs):
            return initial

        def fake_followup_runner(*, tools, **_kwargs):
            from codey.research.source_document import SourceDocument
            tools.sources_read.add("https://example.com/accepted")
            tools.ledger.record_open_document(SourceDocument.html(
                requested_url="https://example.com/accepted",
                final_url="https://example.com/accepted",
                title="Accepted Source",
                text="Accepted text",
            ))

            tools.knowledge_write({
                "type": "fact",
                "title": "Accepted Claim",
                "body": "Accepted body",
                "sources": ["https://example.com/accepted"],
                "evidence": [{
                    "source_url": "https://example.com/accepted",
                    "excerpt": "Accepted text",
                    "claim": "Accepted Claim",
                }],
            })
            return EvidenceFollowupResult(
                ok=True,
                new_evidence_count=1,
                new_source_urls=("https://example.com/accepted",),
                stop_reason="written",
            )

        def fake_review(record, *, question: str = "", evidence_ledger=None, require_ledger_record: bool = False):
            del question, evidence_ledger
            return _review(
                record_id=getattr(record, "record_id", ""),
                record_digest=getattr(record, "record_digest", ""),
                ok=False,
                answer_status="insufficient_evidence",
                score=0.35,
                missing=("answer_coverage_gap",),
            )

        def fake_plan(review, *, max_queries: int, max_sources: int, **_kwargs):
            return ResearchPlan(
                plan_ref="research_plan:gap",
                query_candidates=(QueryCandidate("q:1", "gap query"),),
                reason_codes=("proof_gap",),
                max_queries=max_queries,
                max_sources=max_sources,
            )

        material = PlanExecutionResult(
            queries_executed=("gap query",),
            fresh_source_urls=("https://example.com/accepted",),
            stop_reason="opened_sources",
        )

        context = ResearchContext(
            question="Pipeline question",
            session_id="session-acc",
            run_id="run-acc",
            project="project-acc",
            max_turns=4,
            trace=RunTraceResearchSink(trace),
        )

        from codey.research import pipeline as pipeline_module

        original_review = pipeline_module.review_research_proof
        original_plan = pipeline_module.build_research_plan
        original_execute = pipeline_module.PlanExecutor.execute
        original_selects = pipeline_module._selects_candidate
        try:
            pipeline_module.review_research_proof = fake_review
            pipeline_module.build_research_plan = fake_plan
            pipeline_module.PlanExecutor.execute = lambda self, plan, tools: material
            # Candidate is accepted
            pipeline_module._selects_candidate = lambda cand, cr, best, br: True
            pipeline = ResearchPipeline(
                context=context,
                run_iteration=run_iteration,
                search_factory=lambda: search,
                evidence_followup_runner=fake_followup_runner,
                config=ResearchPipelineConfig(enabled=True, max_followup_rounds=1),
            )
            output = pipeline.run()
        finally:
            pipeline_module.review_research_proof = original_review
            pipeline_module.build_research_plan = original_plan
            pipeline_module.PlanExecutor.execute = original_execute
            pipeline_module._selects_candidate = original_selects

        assert output.followup_applied is True
        assert output.planner_stop_reason in {"evidence_merged", "max_followup_rounds"}
        # Initial tools committed changes to primary store
        assert len(initial_tools.ledger.evidence_items) == 1
        assert "https://example.com/accepted" in initial_tools.sources_read
        assert len(initial_tools.created_ids) == 1
        assert len(list(store.root.glob("**/*.md"))) == 1




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
            return initial

        def raising_followup(**kwargs):
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
            fresh_source_urls=("https://example.com/followup",),
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
                evidence_followup_runner=raising_followup,
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
        assert len(run_calls) == 1


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


def test_pipeline_retains_best_when_staging_commit_fails() -> None:
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
        root = Path(td)
        project = root / "project"
        project.mkdir()
        store = KnowledgeStore(root / "knowledge")
        changes = KnowledgeChanges(root=store.root)
        search = _PipelineSearch()
        initial_tools = ResearchTools(search=search, store=store, changes=changes, session_id="session-comm-fail", project="project-comm-fail")
        trace = _TraceRecorder()
        initial_record = _record(question="Pipeline question", synthesis_id="initial", project=project)
        initial = _result(
            question="Pipeline question",
            summary="initial summary",
            stop_reason="done",
            synthesis_id="initial",
            record=initial_record,
            tools=initial_tools,
        )

        def run_iteration(**_kwargs):
            return initial

        def fake_followup_runner(*, tools, **_kwargs):
            from codey.research.source_document import SourceDocument
            tools.sources_read.add("https://example.com/accepted")
            tools.ledger.record_open_document(SourceDocument.html(
                requested_url="https://example.com/accepted",
                final_url="https://example.com/accepted",
                title="Accepted Source",
                text="Accepted text",
            ))
            tools.knowledge_write({
                "type": "fact",
                "title": "Accepted Claim",
                "body": "Accepted body",
                "sources": ["https://example.com/accepted"],
                "evidence": [{
                    "source_url": "https://example.com/accepted",
                    "excerpt": "Accepted text",
                    "claim": "Accepted Claim",
                }],
            })
            return EvidenceFollowupResult(
                ok=True,
                new_evidence_count=1,
                new_source_urls=("https://example.com/accepted",),
                stop_reason="written",
            )

        def fake_review(record, *, question: str = "", evidence_ledger=None, require_ledger_record: bool = False):
            del question, evidence_ledger
            return _review(
                record_id=getattr(record, "record_id", ""),
                record_digest=getattr(record, "record_digest", ""),
                ok=False,
                answer_status="insufficient_evidence",
                score=0.35,
                missing=("answer_coverage_gap",),
            )

        def fake_plan(review, *, max_queries: int, max_sources: int, **_kwargs):
            return ResearchPlan(
                plan_ref="research_plan:gap",
                query_candidates=(QueryCandidate("q:1", "gap query"),),
                reason_codes=("proof_gap",),
                max_queries=max_queries,
                max_sources=max_sources,
            )

        material = PlanExecutionResult(
            queries_executed=("gap query",),
            fresh_source_urls=("https://example.com/accepted",),
            stop_reason="opened_sources",
        )

        context = ResearchContext(
            question="Pipeline question",
            session_id="session-comm-fail",
            run_id="run-comm-fail",
            project="project-comm-fail",
            max_turns=4,
            trace=RunTraceResearchSink(trace),
        )

        from codey.research import pipeline as pipeline_module

        original_review = pipeline_module.review_research_proof
        original_plan = pipeline_module.build_research_plan
        original_execute = pipeline_module.PlanExecutor.execute
        original_selects = pipeline_module._selects_candidate
        try:
            pipeline_module.review_research_proof = fake_review
            pipeline_module.build_research_plan = fake_plan
            pipeline_module.PlanExecutor.execute = lambda self, plan, tools: material
            pipeline_module._selects_candidate = lambda cand, cr, best, br: True
            # Simulate commit failure in target tools
            initial_tools.commit_staged = lambda staged: (_ for _ in ()).throw(OSError("Disk full during note commit"))
            pipeline = ResearchPipeline(
                context=context,
                run_iteration=run_iteration,
                search_factory=lambda: search,
                evidence_followup_runner=fake_followup_runner,
                config=ResearchPipelineConfig(enabled=True, max_followup_rounds=1),
            )
            output = pipeline.run()
        finally:
            pipeline_module.review_research_proof = original_review
            pipeline_module.build_research_plan = original_plan
            pipeline_module.PlanExecutor.execute = original_execute
            pipeline_module._selects_candidate = original_selects

        # Followup commit failure gracefully preserves the initial successful result
        assert output.final_result is initial.result
        assert output.followup_applied is False
        assert output.planner_stop_reason == "followup_commit_error"


def test_staged_knowledge_store_read_through() -> None:
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
        root = Path(td)
        parent_store = KnowledgeStore(root / "knowledge")
        from codey.knowledge.note import KnowledgeNote
        from codey.research.tools import StagedKnowledgeStore

        # 1. Note in parent
        parent_note = KnowledgeNote(
            id="parent-note",
            title="Parent Title",
            body="Parent Body",
            type="fact",
        )
        parent_store.write_note(parent_note)

        staged_store = StagedKnowledgeStore(parent_store)

        # Read through parent note
        assert staged_store.exists("parent-note") is True
        read_parent = staged_store.read_note("parent-note")
        assert read_parent is not None
        assert read_parent.title == "Parent Title"

        # 2. Note in staging only
        staged_note = KnowledgeNote(
            id="staged-note",
            title="Staged Title",
            body="Staged Body",
            type="fact",
        )
        staged_store.write_note(staged_note)

        assert staged_store.exists("staged-note") is True
        assert parent_store.exists("staged-note") is False
        read_staged = staged_store.read_note("staged-note")
        assert read_staged is not None
        assert read_staged.title == "Staged Title"


def test_staged_knowledge_store_rollback_on_partial_failure() -> None:
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
        root = Path(td)
        target_store = KnowledgeStore(root / "knowledge")
        changes = KnowledgeChanges(root=target_store.root)
        from codey.knowledge.note import KnowledgeNote
        from codey.research.tools import StagedKnowledgeStore

        # Pre-populate an existing note in hypotheses folder
        existing_note = KnowledgeNote(id="existing-note", title="Original Title", body="Original Body", type="hypothesis")
        target_store.write_note(existing_note)
        orig_existing_path = target_store.path_for(existing_note)
        orig_existing_bytes = orig_existing_path.read_bytes()

        staged_store = StagedKnowledgeStore(target_store)

        # 1. New note
        note1 = KnowledgeNote(id="note-1", title="Note 1", body="Body 1", type="fact")
        # 2. Existing note modified and moved from hypothesis to fact folder
        modified_existing = KnowledgeNote(id="existing-note", title="New Title", body="New Body", type="fact")
        # 3. Flaky note that fails
        note3 = KnowledgeNote(id="note-3", title="Note 3", body="Body 3", type="fact")

        staged_store.write_note(note1)
        staged_store.write_note(modified_existing)
        staged_store.write_note(note3)

        original_write = target_store.write_note
        call_count = [0]

        def flaky_write_note(note, *, changes=None):
            call_count[0] += 1
            if call_count[0] == 3:
                raise OSError("Disk full on third note")
            return original_write(note, changes=changes)

        target_store.write_note = flaky_write_note

        # Commit fails on the 3rd note and rolls back note-1 and modified_existing completely
        with pytest.raises(OSError, match="Disk full on third note"):
            staged_store.commit_to(target_store, changes=changes)

        # Verify new note-1 was completely unlinked
        note1_path = target_store.path_for(note1)
        assert note1_path.exists() is False

        # Verify moved path (facts/existing-note.md) was cleaned up, and original (hypotheses/existing-note.md) is 100% byte-exact
        moved_path = target_store.path_for(modified_existing)
        assert moved_path.exists() is False
        assert orig_existing_path.exists() is True
        assert orig_existing_path.read_bytes() == orig_existing_bytes

        # Verify SQLite index is 100% clean
        assert target_store.exists("note-1") is False
        assert target_store.exists("note-3") is False
        assert target_store.exists("existing-note") is True
        assert target_store.index.count() == 1
        idx_existing = target_store.index.get("existing-note")
        assert idx_existing["title"] == "Original Title"
        assert idx_existing["type"] == "hypothesis"

        # Verify KnowledgeChanges is 100% clean: zero false created/updated records
        assert changes.created == []
        assert changes.updated == []
        assert changes.has_changes() is False





def test_staged_knowledge_store_link_validation() -> None:
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
        root = Path(td)
        store = KnowledgeStore(root / "knowledge")
        from codey.knowledge.note import KnowledgeNote
        from codey.research.tools import StagedKnowledgeStore

        staged_store = StagedKnowledgeStore(store)
        staged_store.write_note(KnowledgeNote(id="existing-note", title="T", body="B", type="fact"))

        # Link fails on unknown source
        res1 = staged_store.link("unknown-note", "existing-note")
        assert "ERROR: unknown source note" in res1

        # Link fails on unknown target
        res2 = staged_store.link("existing-note", "unknown-note")
        assert "ERROR: unknown target note" in res2

        # Link succeeds when both exist
        res3 = staged_store.link("existing-note", "existing-note")
        assert res3.startswith("linked:")


def test_content_hash_bytes_consistency() -> None:
    from codey.knowledge.store import _content_hash, content_hash_bytes

    text = "Hello Codey Knowledge Vault\nLine 2"
    raw_bytes = text.encode("utf-8")
    assert _content_hash(text) == content_hash_bytes(raw_bytes)
    assert len(content_hash_bytes(raw_bytes)) == 16


def test_pipeline_tracks_attempted_metrics_when_candidate_rejected() -> None:
    from codey.research.context import NullResearchTraceSink
    from codey.research.plan_executor import PlanExecutionResult


    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
        root = Path(td)
        store = KnowledgeStore(root / "knowledge")
        ledgers = EvidenceLedgerStore(root / "ledgers")
        recorded_pipeline_results = []

        class RecordingTraceSink(NullResearchTraceSink):
            def record_pipeline_result(self, result: object) -> None:
                recorded_pipeline_results.append(result)

        context = ResearchContext(
            question="What is the latency?",
            session_id="s-attempt",
            run_id="r-attempt",
            trace=RecordingTraceSink(),
        )
        tools = ResearchTools(
            search=None,
            store=store,
            changes=KnowledgeChanges(root=store.root),
            session_id="s-attempt",
        )

        initial_result = ResearchRunResult(
            question="What is the latency?",
            summary="Initial summary with low confidence [1].",
            stop_reason="done",
            turns=1,
            sources_read=("https://example.com/init",),
            quality_warnings=("low_confidence",),
        )

        def runner(*, task, max_turns, chat_handoff, search, tools=None, iteration_context=""):
            return ResearchIterationRun(result=initial_result, tools=tools)

        def evidence_runner(*, tools, plan, material, question, initial_summary="", max_context_chars=8000, should_stop=None):
            return EvidenceFollowupResult(
                ok=True,
                new_evidence_count=2,
                stop_reason="done",
            )

        pipeline = ResearchPipeline(
            context=context,
            run_iteration=lambda **kwargs: runner(**kwargs, tools=tools),
            search_factory=lambda: None,
            evidence_followup_runner=evidence_runner,
            evidence_ledgers=ledgers,
        )

        # Force review to have gap initially and then reject candidate
        def reject_candidate(res, require_ledger_record=False):
            if "Initial" in res.summary:
                return ResearchProofReview(
                    ok=False,
                    answers_question=True,
                    answer_status="partial",
                    answer_coverage_score=0.5,
                    citation_present=True,
                    citation_locator_verified=True,
                    support_relation_verified=True,
                    counterevidence_checked=True,
                    ledger_record_verified=True,
                    missing_evidence=("partial_answer",),
                )
            # Make candidate score worse so _selects_candidate returns False
            return ResearchProofReview(
                ok=False,
                answers_question=False,
                answer_status="not_answered",
                answer_coverage_score=0.1,
                citation_present=False,
                citation_locator_verified=False,
                support_relation_verified=False,
                counterevidence_checked=False,
                ledger_record_verified=False,
            )

        pipeline._review = reject_candidate

        # Mock executor to return fresh material
        def mock_executor(plan, staged_tools):
            return PlanExecutionResult(
                fresh_source_count=2,
                fresh_source_urls=("https://example.com/fresh1", "https://example.com/fresh2"),
                stop_reason="queries_budget_exhausted",
            )

        from codey.research.query_planner import QueryCandidate
        pipeline._plan = lambda review: ResearchPlan(
            plan_ref="p1",
            query_candidates=(QueryCandidate(query_id="q1", query_preview="query preview"),),
        )

        with pytest.MonkeyPatch.context() as mp:
            mp.setattr("codey.research.pipeline.PlanExecutor.execute", lambda self, plan, staged_tools: mock_executor(plan, staged_tools))
            pipeline_result = pipeline.run()

        # Followup was attempted but rejected
        assert pipeline_result.followup_applied is False
        assert pipeline_result.planner_stop_reason == "candidate_not_selected"
        # Merged / applied counts are 0
        assert pipeline_result.fresh_source_count == 0
        assert pipeline_result.new_evidence_count == 0
        assert pipeline_result.final_evidence_count == 0
        # Attempted counts are fully observable!
        assert pipeline_result.attempted_fresh_source_count == 2
        assert pipeline_result.attempted_new_evidence_count == 2
        payload = pipeline_result.to_payload()
        assert payload["final_evidence_count"] == 0
        assert payload["attempted_fresh_source_count"] == 2
        assert payload["attempted_new_evidence_count"] == 2
        assert len(recorded_pipeline_results) == 1
        assert recorded_pipeline_results[0].final_evidence_count == 0
        assert recorded_pipeline_results[0].attempted_fresh_source_count == 2
        assert recorded_pipeline_results[0].attempted_new_evidence_count == 2
