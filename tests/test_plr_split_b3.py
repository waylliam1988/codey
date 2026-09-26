"""Regression lock for the pure-extractive PLR0912/PLR0915 split (b3).

Covers only behavior preservation of the extracted helpers in
``codey/research/runner.py``, ``codey/research/plan_executor.py`` and
``codey/research/pipeline.py``. No behavior change is expected.
"""

from __future__ import annotations

import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from codey.knowledge.store import KnowledgeStore
from codey.research.context import ResearchPipelineConfig
from codey.research.pipeline import ResearchPipeline
from codey.research.plan_executor import PlanExecutor, _PlanExecutionState
from codey.research.query_planner import QueryCandidate, ResearchPlan
from codey.research.runner import ResearchRunner


class _Provider:
    name = "Fake"
    location = "fake://provider"

    def __init__(self) -> None:
        self.sent: list[str] = []

    def new_chat(self, timeout=None) -> None:
        return None

    def send(self, text: str, timeout=None) -> str:
        self.sent.append(text)
        return '{"tool": "done", "args": {"answer": "x"}}'


class _Search:
    name = "fake-search"

    def search(self, query: str, limit: int = 8) -> list[dict]:
        return []

    def fetch(self, url: str) -> dict:
        return {"url": url, "title": "t", "text": "body", "truncated": False}

    def close(self) -> None:
        return None


def _runner() -> tuple[ResearchRunner, KnowledgeStore, tempfile.TemporaryDirectory]:
    td = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
    store = KnowledgeStore(Path(td.name) / "knowledge")
    runner = ResearchRunner(_Provider(), _Search(), store, max_turns=2, controller_enabled=False)
    return runner, store, td


def test_prepare_run_empty_question_preserves_yield_order() -> None:
    runner, store, td = _runner()
    try:
        events = list(runner._prepare_run("   "))
        assert len(events) == 2
        assert runner.result is not None
        assert runner.result.stop_reason == "empty"
        full = list(runner.run("  "))
        assert len(full) == 2
    finally:
        store.index.close()
        td.cleanup()


def test_prepare_run_ready_path_emits_no_events() -> None:
    runner, store, td = _runner()
    try:
        events = list(runner._prepare_run("hello"))
        assert events == []
    finally:
        store.index.close()
        td.cleanup()


def test_protocol_step_repair_then_breaks_on_third_error() -> None:
    runner, store, td = _runner()
    try:
        plan = SimpleNamespace(
            protocol_error="bad",
            protocol_error_kind="no_json",
            protocol_tool_name="",
            calls=[],
            control=None,
        )
        first = runner._protocol_step(plan, turn=1, protocol_errors=0, control_state=None)
        assert first.action == "continue"
        assert first.protocol_errors == 1
        assert first.message
        third = runner._protocol_step(plan, turn=1, protocol_errors=2, control_state=None)
        assert third.action == "break"
        assert third.protocol_errors == 3
        valid = SimpleNamespace(protocol_error="", calls=[SimpleNamespace(name="x")], control=None)
        ok = runner._protocol_step(valid, turn=1, protocol_errors=2, control_state=None)
        assert ok.action == "proceed"
        assert ok.protocol_errors == 0
    finally:
        store.index.close()
        td.cleanup()


def test_step_idle_no_calls_uses_repair_prompt() -> None:
    runner, store, td = _runner()
    try:
        plan = SimpleNamespace(calls=[])
        step = runner._step_idle(plan, [], idle_turns=0)
        assert step.flow == "continue"
        assert step.message == runner.codec.repair_prompt()
        assert step.idle_turns == 1
        full = runner._step_idle(plan, [], idle_turns=2)
        assert full.flow == "break"
        assert full.stop_reason == "no_progress"
    finally:
        store.index.close()
        td.cleanup()


def test_step_idle_none_message_continues_empty() -> None:
    runner, store, td = _runner()
    try:
        plan = SimpleNamespace(calls=[SimpleNamespace(name="web_search")])
        with mock.patch("codey.research.native_bridge.next_message", return_value=None):
            step = runner._step_idle(plan, [], idle_turns=0)
        assert step.flow == "continue"
        assert step.message == ""
    finally:
        store.index.close()
        td.cleanup()


def test_plan_executor_limits_and_finalize() -> None:
    executor = PlanExecutor(
        config=ResearchPipelineConfig(
            max_queries_per_round=2,
            max_total_sources=2,
            max_sources_per_query=1,
        )
    )
    plan = ResearchPlan(
        plan_ref="research_plan:" + "a" * 16,
        query_candidates=(QueryCandidate("research_query:" + "1" * 16, "q"),),
        max_queries=3,
        max_sources=3,
    )
    assert executor._execution_limits(plan) == (2, 2, 1)
    state = _PlanExecutionState(
        queries=["q"],
        opened=[{"a": 1}, {"b": 2}],
        previews=[],
        fresh_urls=["https://example.com/x"],
        errors=[],
        skipped=0,
        seen_urls=set(),
        baseline_urls=set(),
        stop_reason="opened_sources",
    )
    assert executor._finalize_stop_reason(state, 2) == "max_sources"
    state2 = _PlanExecutionState(
        queries=["q"],
        opened=[],
        previews=[],
        fresh_urls=[],
        errors=[],
        skipped=1,
        seen_urls=set(),
        baseline_urls=set(),
        stop_reason="no_queries",
    )
    assert executor._finalize_stop_reason(state2, 2) == "no_new_material"


def test_pipeline_drive_followup_missing_tools() -> None:
    pipeline = ResearchPipeline(
        context=mock.MagicMock(),
        run_iteration=mock.MagicMock(),
        search_factory=lambda: mock.MagicMock(),
    )
    plan = ResearchPlan(
        plan_ref="research_plan:" + "b" * 16,
        query_candidates=(),
        max_queries=1,
        max_sources=1,
    )
    best = SimpleNamespace(
        question="q",
        summary="",
        stop_reason="done",
        research_record=None,
    )
    outcome = pipeline._drive_followup(
        plan=plan,
        best=best,
        best_review=None,
        best_tools=None,
        followup_rounds=0,
        total_fresh_sources=0,
        total_new_evidence=0,
        total_attempted_fresh_sources=0,
        total_attempted_new_evidence=0,
        total_merged_evidence=0,
        planner_stop_reason="planned",
    )
    assert outcome.planner_stop_reason == "missing_iteration_tools"
    assert outcome.followup_rounds == 0


def test_extracted_helpers_exist() -> None:
    for name in (
        "_prepare_run",
        "_build_turn_outbound",
        "_protocol_step",
        "_run_tool_calls",
        "_step_done",
        "_step_idle",
        "_maybe_persist_synthesis",
        "_record_final_traces",
    ):
        assert hasattr(ResearchRunner, name), name
    for name in ("_execution_limits", "_drain_search_hits", "_finalize_stop_reason"):
        assert hasattr(PlanExecutor, name), name
    for name in ("_drive_followup", "_finalize_pipeline_result"):
        assert hasattr(ResearchPipeline, name), name
