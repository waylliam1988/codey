"""Tool facts must persist in the receipt canonical and replay identically.

source_search hit mapping, opened final URL, and knowledge_write evidence
refs live in ToolResult.canonical. Live facts and recovered facts use the
same record_facts_for_result path; recovery never re-networks.
"""
from __future__ import annotations


def _policy():
    from codey.policies.task_policy import TaskPolicy

    return TaskPolicy(grants=frozenset({"control", "web.read", "knowledge.read", "knowledge.write"}))


def _session():
    from codey.operations.task_session import TaskSession

    return TaskSession(policy=_policy(), task_kind="research", project="", max_turns=4)


def test_source_search_canonical_replays_to_same_facts():
    from codey.operations.kernel_facts import record_facts_for_result
    from codey.operations.task_execution import ExecutionDelegate
    from codey.runtime.core.models import ToolCall

    session = _session()

    class FakeSearch:
        def source_search(self, url, query, limit=6):
            return "1. offset 120: matched text\n2. p.3: other"

    delegate = ExecutionDelegate(session=session, research_tools=type("R", (), {"source_search": FakeSearch().source_search})())
    call = ToolCall("source_search", {"url": "https://example.com/a", "query": "q"})
    result, ok, exit_code = delegate._execute_research(call, turn=1, tool_index=0)
    assert ok is True
    assert "h1" in result.model_text
    assert isinstance(result.canonical, dict)
    assert "hit_targets" in result.canonical
    assert result.canonical["hit_targets"]["h1"]["url"] == "https://example.com/a"

    # Live facts via the single entry.
    record_facts_for_result(session, call, result, ok=ok)
    live_targets = dict(session.hit_targets)
    assert live_targets["h1"]["offset"] == 120

    # Recovered facts from the same receipt canonical must match live.
    fresh = _session()
    record_facts_for_result(fresh, call, result, ok=ok)
    assert fresh.hit_targets == live_targets
    # Same id same target replays; same id different target must fail.
    record_facts_for_result(fresh, call, result, ok=ok)
    from codey.runtime.core.models import ToolResult

    tampered = ToolResult(
        call=call, model_text=result.model_text,
        canonical={"hit_targets": {"h1": {"url": "https://evil.example/", "offset": 0, "pages": ""}}},
    )
    try:
        record_facts_for_result(fresh, call, tampered, ok=True)
    except Exception:
        pass
    else:
        raise AssertionError("conflicting hit target must raise")


def test_open_hit_resolves_from_hit_targets_fact():
    from codey.operations.kernel_facts import record_facts_for_result
    from codey.runtime.core.models import ToolCall, ToolResult

    session = _session()
    search_call = ToolCall("source_search", {"url": "https://example.com/a", "query": "q"})
    search_result = ToolResult(
        call=search_call, model_text="h1: 1. offset 120: matched text",
        canonical={"hit_targets": {"h1": {"url": "https://example.com/a", "offset": 120, "pages": ""}}},
    )
    record_facts_for_result(session, search_call, search_result, ok=True)
    open_call = ToolCall("open_hit", {"hit_id": "h1"})
    open_result = ToolResult(
        call=open_call, model_text="Title: A\nbody",
        canonical={"opened_url": "https://example.com/a", "request_url": "h1"},
    )
    record_facts_for_result(session, open_call, open_result, ok=True)
    assert "https://example.com/a" in session.opened_sources


def test_knowledge_write_canonical_replays_evidence():
    from codey.operations.kernel_facts import record_facts_for_result
    from codey.runtime.core.models import ToolCall, ToolResult

    session = _session()
    call = ToolCall("knowledge_write", {"sources": ["https://example.com/a"]})
    result = ToolResult(
        call=call, model_text="saved",
        canonical={"evidence_items": [{"source_url": "https://example.com/a", "excerpt": "clip"}]},
    )
    record_facts_for_result(session, call, result, ok=True)
    assert session.evidence == [{"source_url": "https://example.com/a", "excerpt": "clip"}]
