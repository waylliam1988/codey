"""Research 合成写入授权与幂等：拒绝缺席策略，重试复用同一笔记。"""

from __future__ import annotations

import tempfile
from pathlib import Path

import pytest

from codey.knowledge.changes import KnowledgeChanges
from codey.knowledge.store import KnowledgeStore
from codey.policies.task_policy import TaskPolicy
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


def _write_policy() -> TaskPolicy:
    return TaskPolicy(grants=frozenset({
        "control", "web.read", "knowledge.read", "knowledge.write", "knowledge.link",
    }))


def _knowledge_files(store: KnowledgeStore) -> list[Path]:
    return [p for p in Path(store.root).rglob("*.md") if p.is_file()]


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
    assert len(tools.store.documents.all()) == 1
    assert len(tools.store.documents.history(first)) == 1
