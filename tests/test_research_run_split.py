"""Regression lock for the pure-extractive PLR0912/PLR0915 split of research run flow.

Covers behavior preservation of the research helpers in
``codey/operations/research_iteration.py``, ``codey/research/plan_executor.py``
and ``codey/research/pipeline.py``. No behavior change is expected.
"""

from __future__ import annotations

import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from codey.knowledge.store import KnowledgeStore
from codey.operations.research_iteration import ResearchIteration
from codey.research.context import ResearchPipelineConfig
from codey.research.pipeline import ResearchPipeline
from codey.research.plan_executor import PlanExecutor, _PlanExecutionState
from codey.research.query_planner import QueryCandidate, ResearchPlan


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


def _runner() -> tuple[ResearchIteration, KnowledgeStore, tempfile.TemporaryDirectory]:
    td = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
    store = KnowledgeStore(Path(td.name) / "knowledge")
    runner = ResearchIteration(_Provider(), _Search(), store, max_turns=2, controller_enabled=False)
    return runner, store, td


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
