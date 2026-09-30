"""Research 系统动作授权闭合与合成幂等（真实回调/真实存储）。

- 策略参数必填：缺席不得默认放行，None 必须拒绝；
- 管线不得去掉策略重试：回调先产生效果再抛 parent_policy/TypeError 时，
  必须向上传播，不得无策略再调一次；
- 合成写入成功后重试必须复用同一笔记 ID，不得产生两条笔记。
"""

from __future__ import annotations

import tempfile
from pathlib import Path

import pytest

from codey.knowledge.changes import KnowledgeChanges
from codey.knowledge.store import KnowledgeStore
from codey.policies.task_policy import TaskPolicy
from codey.research.context import ResearchContext, ResearchPipelineConfig
from codey.research.plan_executor import PlanExecutor
from codey.research.query_planner import QueryCandidate, ResearchPlan
from codey.research.tools import ResearchTools


class _CountingSearch:
    def __init__(self):
        self.queries: list[str] = []
        self.closed = False

    def search(self, query: str, limit: int = 6):
        self.queries.append(query)
        return []

    def close(self):
        self.closed = True


def _make_tools(search, session_id="s-sysact") -> ResearchTools:
    tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
    root = Path(tmp.name)
    store = KnowledgeStore(root / "knowledge")
    tools = ResearchTools(
        search=search, store=store, changes=KnowledgeChanges(root=store.root),
        session_id=session_id, project="",
    )
    tools._tmp_guard = tmp  # type: ignore[attr-defined]
    return tools


def _plan() -> ResearchPlan:
    return ResearchPlan(
        plan_ref="plan-sysact",
        query_candidates=(QueryCandidate(query_id="q1", query_preview="python docs"),),
        max_queries=1,
        max_sources=2,
    )


def _write_policy() -> TaskPolicy:
    return TaskPolicy(grants=frozenset({
        "control", "web.read", "knowledge.read", "knowledge.write", "knowledge.link",
    }))


def _knowledge_files(store: KnowledgeStore) -> list[Path]:
    return [p for p in Path(store.root).rglob("*.md") if p.is_file()]


def test_plan_executor_policy_is_required_and_none_is_denied():
    search = _CountingSearch()
    tools = _make_tools(search)
    executor = PlanExecutor(config=ResearchPipelineConfig())
    with pytest.raises(TypeError):
        executor.execute(_plan(), tools)  # type: ignore[call-arg]
    result = executor.execute(_plan(), tools, policy=None)
    assert search.queries == []
    assert result.stop_reason == "policy_denied"


def test_persist_synthesis_policy_is_required_and_none_is_denied():
    from codey.operations.research_iteration import _persist_synthesis

    search = _CountingSearch()
    tools = _make_tools(search)
    with pytest.raises(TypeError):
        _persist_synthesis(  # type: ignore[call-arg]
            tools, "q", "summary", session_id="s", project="",
            run_id="run-syn-1", on_event=lambda _e: None,
        )
    events: list[object] = []
    note_id = _persist_synthesis(
        tools, "q", "summary", session_id="s", project="",
        run_id="run-syn-1", on_event=events.append, policy=None,
    )
    assert note_id == ""
    assert list(tools.created_ids) == []


def test_subset_followup_policy_without_parent_write_grants_nothing():
    from codey.operations.evidence_followup import _subset_followup_policy

    policy = _subset_followup_policy(None)
    assert policy.allows("control") is True
    assert policy.allows("knowledge.write") is False


def test_subset_followup_policy_honors_parent_explicit_denials():
    from codey.operations.evidence_followup import _subset_followup_policy

    parent = TaskPolicy(grants=frozenset({"control", "knowledge.write"}),
                        denied_capabilities=frozenset({"knowledge.write"}))
    assert not _subset_followup_policy(parent).allows("knowledge.write")


def test_pipeline_propagates_parent_policy_type_error_without_retry():
    from codey.research.pipeline import ResearchPipeline
    from codey.research.run_result import ResearchRunResult

    search = _CountingSearch()
    tools = _make_tools(search)
    result = ResearchRunResult(question="q", summary="s", stop_reason="done", turns=1)

    def run_iteration(**_kwargs):
        from codey.research.pipeline import ResearchIterationRun

        return ResearchIterationRun(result=result, tools=tools)

    calls: list[dict] = []

    def flaky_followup(**kwargs):
        calls.append(kwargs)
        raise TypeError("unexpected keyword argument 'parent_policy'")

    from codey.research.context import RunTraceResearchSink

    class _NullTrace:
        def __getattr__(self, name: str):
            def call(*args, **kwargs) -> None:
                return None
            return call

    context = ResearchContext(
        question="q", session_id="s-pipe", run_id="r-pipe", project="",
        proof_question="q", max_turns=1, should_stop=lambda: False,
        trace=RunTraceResearchSink(_NullTrace()),
    )
    pipeline = ResearchPipeline(
        context=context,
        run_iteration=run_iteration,  # type: ignore[arg-type]
        search_factory=lambda: search,
        evidence_followup_runner=flaky_followup,  # type: ignore[arg-type]
        config=ResearchPipelineConfig(enabled=True, max_followup_rounds=1),
        policy=_write_policy(),
    )
    # 必须直接传播，不得去掉 parent_policy 再调一次（calls 仍为实际调用次数，
    # 且异常向上传播而不是被吞掉重试）
    with pytest.raises(TypeError):
        pipeline._run_followup(tools, _plan(), mock_material(), result)
    assert len(calls) == 1
    assert "parent_policy" in calls[0]


def mock_material():
    from codey.research.plan_executor import PlanExecutionResult

    return PlanExecutionResult(
        queries_executed=("q",), opened_sources=(), previews=(),
        fresh_source_urls=("https://example.com/x",), fresh_source_count=1,
        baseline_source_urls=(), skipped_count=0,
        stop_reason="opened_sources", errors=(),
    )


def test_synthesis_write_then_retry_keeps_single_note():
    from codey.operations.research_iteration import _persist_synthesis
    from codey.research.source_document import SourceDocument

    search = _CountingSearch()
    tools = _make_tools(search)
    tools.sources_read.add("https://example.com/a")
    tools.ledger.record_open_document(SourceDocument.html(
        requested_url="https://example.com/a", final_url="https://example.com/a",
        title="A", text="body text for synthesis",
    ))
    policy = _write_policy()
    events: list[object] = []
    first = _persist_synthesis(
        tools, "question", "summary text", session_id="s-syn",
        project="", run_id="run-syn-stable", on_event=events.append, policy=policy,
    )
    assert first, "首次写入应成功"
    # 模拟“写入成功后交付失败”的重试：同一 run 再次合成不得产生第二条笔记
    tools2 = _make_tools(search)
    tools2.store = tools.store
    tools2.changes = tools.changes
    tools2.sources_read.add("https://example.com/a")
    tools2.ledger.record_open_document(SourceDocument.html(
        requested_url="https://example.com/a", final_url="https://example.com/a",
        title="A", text="body text for synthesis",
    ))
    second = _persist_synthesis(
        tools2, "question", "summary text", session_id="s-syn",
        project="", run_id="run-syn-stable", on_event=events.append, policy=policy,
    )
    assert second == first, f"同一 run 重试必须复用原笔记 ID：{first} vs {second}"
    assert len(_knowledge_files(tools.store)) == 1
