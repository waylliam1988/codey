"""Locators are semantic identities, never display clips."""
from __future__ import annotations

import pytest

from codey.operations.kernel_facts import bind_source_identity, record_facts_for_result
from codey.operations.task_execution import ExecutionDelegate
from codey.operations.task_session import TaskSession
from codey.policies.task_policy import TaskPolicy
from codey.research.ledger import ResearchLedger
from codey.research.ledger_receipts import ledger_observation
from codey.research.source_snapshots import snapshot_document
from codey.research.tools import ResearchToolOutput, ResearchTools
from codey.runtime.core.models import ToolCall, ToolResult
from codey.types.json_value import JsonObject, decode_json_object


def session() -> TaskSession:
    return TaskSession(policy=TaskPolicy(grants=frozenset({"control", "web.read", "knowledge.write"})))


def test_long_search_open_and_evidence_keep_distinct_urls() -> None:
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


def test_long_hit_is_opened_and_replayed_with_exact_target() -> None:
    url = "https://example.com/?token=" + "x" * 700
    task = session()
    seen: list[tuple[str, dict[str, int | str]]] = []
    ledger = ResearchLedger()

    class Tools(ResearchTools):
        def read_source(self, observation: JsonObject, *, offset: int = 0, limit: int = 6000,
                        pages: str = "") -> ResearchToolOutput:
            seen.append((snapshot_document(observation).final_url, {"offset": offset, "limit": limit, "pages": pages}))
            return super().read_source(observation, offset=offset, limit=limit, pages=pages)

        def source_search(self, url: str, query: str, limit: object = 6) -> ResearchToolOutput:
            self.ledger.record_source_search(url, query, [{"offset": 120, "page": None, "snippet": "observed"}])
            return ResearchToolOutput("1. offset 999: unrelated display", ok=True)

    ledger.record_open(url, url, "Test", "observed text\n" * 100)
    tools = Tools(search=None, store=None, changes=None, ledger=ledger)
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
    assert seen == [(url, {"offset": 120, "limit": 6000, "pages": ""})]
    document = snapshot_document(decode_json_object(opened.canonical["research_observation"]))
    assert document.final_url == url and document.requested_url == url
    opened = bind_source_identity(fresh, opened.call, opened, ok=True)
    record_facts_for_result(fresh, opened.call, opened, ok=True)
    assert fresh.opened_sources == {url}


def test_unrelated_ledger_source_cannot_replace_actual_open() -> None:
    requested = "https://example.com/requested"
    unrelated = "https://example.com/unrelated"
    ledger = ResearchLedger()
    ledger.record_open(unrelated, unrelated, "Unrelated", "other content")
    delegate = ExecutionDelegate(research_tools=ResearchTools(None, None, None, ledger=ledger))
    with pytest.raises(ValueError, match="missing research observation"):
        delegate._opened_result(ToolCall("open_url", {"url": requested}), ResearchToolOutput("body", ok=True), requested, ledger=ledger, turn=1, tool_index=0)


def test_collision_after_500_characters_is_still_a_conflict() -> None:
    task = session()
    url = "https://example.com/" + "x" * 600
    call = ToolCall("source_search", {"url": url, "query": "q"})
    def result(target: str) -> ToolResult:
        ledger = ResearchLedger()
        ledger.record_open(target, target, "Captured", "body")
        snapshot = ledger_observation(ledger, "open", url=target)
        return ToolResult(call, "hit", ok=True, canonical={
            "hit_targets": {"h1": {"url": target, "offset": 0, "pages": "", "text_hash": decode_json_object(snapshot["source"])["text_hash"]}},
            "research_observation": {"source_search": {"source_url": target, "query": "q", "hits": [{"offset": 0, "page": None, "snippet": "body"}], "source_snapshot": snapshot}},
        })
    record_facts_for_result(task, call, result(url + "one"), ok=True)
    with pytest.raises(ValueError, match="conflicting"):
        record_facts_for_result(task, call, result(url + "two"), ok=True)
    assert task.hit_targets["h1"]["url"] == url + "one"



def test_search_source_identity_comes_from_receipt_not_rendered_punctuation() -> None:
    task = session()
    ledger = ResearchLedger()
    url = "https://example.com/entry_(official)"
    class Tools(ResearchTools):
        def web_search(self, query: str) -> ResearchToolOutput:
            self.ledger.record_search(query, [{"title": "Official", "url": url, "snippet": "See https://unrelated.example/a"}])
            return ResearchToolOutput(f"1. Official\n   {url}\n   See https://unrelated.example/a", ok=True)
    call = ToolCall("web_search", {"query": "official"})
    tools = Tools(None, None, None, ledger=ledger)
    result, ok, _ = ExecutionDelegate(session=task, research_tools=tools)._execute_research(call)
    assert ok is True
    record_facts_for_result(task, call, result, ok=True)
    assert task.search_results == {"r1": url}



def test_malformed_search_receipt_cannot_mutate_facts_or_use_display_fallback() -> None:
    task = session()
    call = ToolCall("web_search", {"query": "official"})
    ledger = ResearchLedger()
    ledger.record_search("official", [{"url": "https://example.com/valid"}, {"url": "https://example.com/bad"}])
    observation = ledger_observation(ledger, "web_search")
    search = decode_json_object(observation["search"])
    results = search["results"]
    assert isinstance(results, list)
    results[1] = {**decode_json_object(results[1]), "url": False}
    observation["search"] = {**search, "results": results}
    result = ToolResult(call, "https://example.com/fallback", ok=True,
                        canonical={"research_observation": observation})
    with pytest.raises(ValueError, match="URL"):
        record_facts_for_result(task, call, result, ok=True)
    assert task.searches == []
    assert task.search_results == {}
