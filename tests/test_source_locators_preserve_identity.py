"""Locators are semantic identities, never display clips."""
from types import SimpleNamespace

from codey.operations.kernel_facts import record_facts_for_result
from codey.operations.task_execution import ExecutionDelegate
from codey.operations.task_session import TaskSession
from codey.policies.task_policy import TaskPolicy
from codey.research.tools import ResearchToolOutput
from codey.runtime.core.models import ToolCall, ToolResult


def session():
    return TaskSession(policy=TaskPolicy(grants=frozenset({"control", "web.read", "knowledge.write"})))


def test_long_search_open_and_evidence_keep_distinct_urls():
    first = "https://example.com/?query=" + "a" * 600 + "first"
    second = first[:-5] + "second"
    task = session()
    task.record_search_result("r1", first)
    task.record_search_result("r2", second)
    task.record_open(first)
    task.record_open(second)
    task.record_evidence(first, "observed")
    assert task.search_results == {"r1": first, "r2": second}
    assert task.opened_sources == {first, second}
    assert task.source_ids == {"s1": first, "s2": second}
    assert task.evidence[0]["source_url"] == first


def test_long_hit_is_opened_and_replayed_with_exact_target():
    url = "https://example.com/?token=" + "x" * 700
    task = session()
    seen = []
    tools = SimpleNamespace(
        source_search=lambda *args: ResearchToolOutput("1. offset 120: observed", ok=True),
        open_url=lambda actual, **kw: seen.append((actual, kw)) or ResearchToolOutput("Title: Test\nbody", ok=True),
    )
    delegate = ExecutionDelegate(session=task, research_tools=tools)
    call = ToolCall("source_search", {"url": url, "query": "q"})
    result, ok, _ = delegate._execute_research(call)
    assert ok is True
    record_facts_for_result(task, call, result, ok=True)
    fresh = session()
    record_facts_for_result(fresh, call, result, ok=True)
    assert fresh.hit_targets["h1"]["url"] == url
    opened, ok, _ = ExecutionDelegate(session=fresh, research_tools=tools)._execute_research(
        ToolCall("open_hit", {"hit_id": "h1"}),
    )
    assert ok is True
    assert seen == [(url, {"offset": 120, "pages": ""})]
    assert opened.canonical["opened_url"] == url
    assert opened.canonical["request_url"] == url
    record_facts_for_result(fresh, opened.call, opened, ok=True)
    assert fresh.opened_sources == {url}


def test_unrelated_ledger_source_cannot_replace_actual_open():
    requested = "https://example.com/requested"
    unrelated = "https://example.com/unrelated"
    ledger = SimpleNamespace(final_url_set=lambda: {unrelated}, canonical_opened_url=lambda _: "")
    delegate = ExecutionDelegate(research_tools=SimpleNamespace(ledger=ledger))
    result, ok, _ = delegate._opened_result(ToolCall("open_url", {"url": requested}), ResearchToolOutput("body", ok=True), requested, turn=1, tool_index=0)
    assert ok is True
    assert result.canonical["opened_url"] == requested


def test_collision_after_500_characters_is_still_a_conflict():
    import pytest

    task = session()
    url = "https://example.com/" + "x" * 600
    call = ToolCall("source_search", {"url": url, "query": "q"})
    def result(target):
        return ToolResult(call, "hit", ok=True, canonical={"hit_targets": {"h1": {"url": target, "offset": 0, "pages": ""}}})
    record_facts_for_result(task, call, result(url + "one"), ok=True)
    with pytest.raises(ValueError, match="conflicting"):
        record_facts_for_result(task, call, result(url + "two"), ok=True)
    assert task.hit_targets["h1"]["url"] == url + "one"



def test_search_source_identity_comes_from_receipt_not_rendered_punctuation():
    from codey.research.ledger import ResearchLedger

    task = session()
    ledger = ResearchLedger()
    url = "https://example.com/entry_(official)"
    def search(query):
        ledger.record_search(query, [{"title": "Official", "url": url, "snippet": "See https://unrelated.example/a"}])
        return ResearchToolOutput(f"1. Official\n   {url}\n   See https://unrelated.example/a", ok=True)
    call = ToolCall("web_search", {"query": "official"})
    result, ok, _ = ExecutionDelegate(session=task, research_tools=SimpleNamespace(ledger=ledger, web_search=search))._execute_research(call)
    assert ok is True
    record_facts_for_result(task, call, result, ok=True)
    assert task.search_results == {"r1": url}



def test_malformed_search_receipt_cannot_mutate_facts_or_use_display_fallback():
    import pytest

    task = session()
    call = ToolCall("web_search", {"query": "official"})
    result = ToolResult(call, "https://example.com/fallback", ok=True, canonical={
        "research_observation": {"search": {"results": [{"url": "https://example.com/valid"}, {"url": False}]}}
    })
    with pytest.raises(ValueError, match="URL"):
        record_facts_for_result(task, call, result, ok=True)
    assert task.searches == []
    assert task.search_results == {}
