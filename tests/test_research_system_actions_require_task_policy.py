"""Research系统动作必须遵守任务授权（控制面只读不得联网/写笔记）。"""
from __future__ import annotations

import tempfile
from pathlib import Path

from codey.knowledge.changes import KnowledgeChanges
from codey.knowledge.store import KnowledgeStore
from codey.policies.task_policy import TaskPolicy
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
        project="", run_id="run-deny-syn", on_event=events.append, policy=policy,
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
        run_id="run-webread-syn", on_event=events.append, policy=policy,
    )
    assert note_id == ""
