"""Research系统动作必须遵守任务授权（控制面只读不得联网/写笔记）。"""
from __future__ import annotations

import tempfile
from pathlib import Path

from codey.knowledge.changes import KnowledgeChanges
from codey.knowledge.store import KnowledgeStore
from codey.policies.task_policy import TaskPolicy
from codey.research.context import ResearchPipelineConfig
from codey.research.plan_executor import PlanExecutor
from codey.research.query_planner import QueryCandidate, ResearchPlan
from codey.research.tools import ResearchTools


class _CountingSearch:
    def __init__(self):
        self.queries: list[str] = []

    def search(self, query: str, limit: int = 6):
        self.queries.append(query)
        return []

    def close(self):
        pass


def _make_tools(search) -> ResearchTools:
    tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
    root = Path(tmp.name)
    store = KnowledgeStore(root / "knowledge")
    tools = ResearchTools(
        search=search,
        store=store,
        changes=KnowledgeChanges(root=store.root),
        session_id="s-policy",
        project="",
    )
    tools._tmp_guard = tmp  # type: ignore[attr-defined]
    return tools


def _plan_with_query() -> ResearchPlan:
    return ResearchPlan(
        plan_ref="plan-policy-test",
        query_candidates=(QueryCandidate(query_id="q1", query_preview="python docs"),),
        max_queries=1,
        max_sources=2,
    )


def test_control_only_plan_executor_must_not_call_search_adapter() -> None:
    search = _CountingSearch()
    tools = _make_tools(search)
    policy = TaskPolicy(grants=frozenset({"control"}))
    executor = PlanExecutor(config=ResearchPipelineConfig())
    result = executor.execute(_plan_with_query(), tools, policy=policy)
    assert search.queries == [], f"control-only 不得搜索，实际调用了 {search.queries}"
    assert result.queries_executed == ()
    assert "policy" in (result.stop_reason or "").lower() or result.stop_reason in {"policy_denied", "stopped", "no_queries"}


def test_control_only_synthesis_must_not_write_note() -> None:
    from codey.operations.research_iteration import _persist_synthesis

    search = _CountingSearch()
    tools = _make_tools(search)
    tools.sources_read.add("https://example.com/a")
    from codey.research.source_document import SourceDocument
    tools.ledger.record_open_document(SourceDocument.html(
        requested_url="https://example.com/a",
        final_url="https://example.com/a",
        title="A",
        text="body text for synthesis",
    ))
    policy = TaskPolicy(grants=frozenset({"control"}))
    events: list[object] = []
    note_id = _persist_synthesis(
        tools, "question", "summary text", session_id="s-policy",
        project="", on_event=events.append, policy=policy,
    )
    assert note_id == ""
    assert list(tools.created_ids) == []


def test_webread_without_knowledge_write_reads_but_saves_no_note() -> None:
    from codey.operations.research_iteration import _persist_synthesis

    search = _CountingSearch()
    tools = _make_tools(search)
    tools.sources_read.add("https://example.com/a")
    from codey.research.source_document import SourceDocument
    tools.ledger.record_open_document(SourceDocument.html(
        requested_url="https://example.com/a",
        final_url="https://example.com/a",
        title="A",
        text="body",
    ))
    policy = TaskPolicy(grants=frozenset({"control", "web.read", "knowledge.read"}))
    events: list[object] = []
    note_id = _persist_synthesis(
        tools, "q", "summary", session_id="s", project="",
        on_event=events.append, policy=policy,
    )
    assert note_id == ""


def test_evidence_followup_policy_is_subset_of_parent() -> None:
    from codey.operations.evidence_followup import run_evidence_followup
    from codey.research.plan_executor import PlanExecutionResult

    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
        root = Path(td)
        store = KnowledgeStore(root / "knowledge")
        tools = ResearchTools(
            search=_CountingSearch(),
            store=store,
            changes=KnowledgeChanges(root=store.root),
            session_id="s-follow",
            project="",
        )
        tools.sources_read.add("https://example.com/fresh")
        from codey.research.source_document import SourceDocument
        tools.ledger.record_open_document(SourceDocument.html(
            requested_url="https://example.com/fresh",
            final_url="https://example.com/fresh",
            title="Fresh",
            text="fresh body",
        ))
        parent = TaskPolicy(grants=frozenset({"control"}))

        class _DoneProvider:
            def send(self, prompt: str) -> str:
                return '{"tool":"knowledge_write","args":{"type":"fact","title":"t","body":"b","sources":["https://example.com/fresh"],"evidence":[{"source_url":"https://example.com/fresh","excerpt":"fresh body","claim":"t","stance":"supports"}]}}'

        plan = ResearchPlan(plan_ref="p", query_candidates=(), max_queries=0, max_sources=0)
        material = PlanExecutionResult(
            queries_executed=(), opened_sources=(), previews=("fresh body",),
            fresh_source_urls=("https://example.com/fresh",), fresh_source_count=1,
            baseline_source_urls=(), skipped_count=0, stop_reason="opened_sources", errors=(),
        )
        result = run_evidence_followup(
            provider=_DoneProvider(), tools=tools, plan=plan, material=material,
            question="q", parent_policy=parent,
        )
        assert not result.ok
        assert list(tools.created_ids) == []
