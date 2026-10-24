"""Successful source facts require the same exact observations on live and replay paths."""
from copy import deepcopy
from dataclasses import asdict
from types import SimpleNamespace

import pytest

from codey.operations.kernel_facts import record_facts_for_result
from codey.operations.task_execution import ExecutionDelegate
from codey.operations.task_session import TaskSession
from codey.research.ledger import ResearchLedger, SearchRecord, SearchResultRecord
from codey.research.ledger_receipts import ledger_observation, restore_ledger_observation
from codey.research.tools import ResearchToolOutput
from codey.runtime.core.models import ToolCall, ToolResult

URL = "https://example.com/requested"
FINAL = "https://example.com/final_(official)"


def search_receipt():
    return {"search": asdict(SearchRecord("observed query", "2026-10-24T00:00:00+00:00", (
        SearchResultRecord(1, "Official", FINAL, "See https://unrelated.example/help"),
    )))}


def source_receipt():
    ledger = ResearchLedger()
    ledger.record_open(URL, FINAL, "Official", "exact observed content")
    return ledger_observation(ledger, "open", url=URL)


@pytest.mark.parametrize("name,args", [
    ("web_search", {"query": "requested query"}),
    ("open_url", {"url": URL}),
    ("open_result", {"result_id": "r1"}),
    ("reopen_source", {"source_id": "s1"}),
    ("open_hit", {"hit_id": "h1"}),
])
def test_no_success_fact_can_be_reconstructed_from_display_parameters_or_aliases(name, args):
    session = TaskSession(policy=None)
    session.search_results["r1"] = URL
    session.source_ids["s1"] = URL
    session.hit_targets["h1"] = {"url": URL, "offset": 0, "pages": ""}
    before = deepcopy(vars(session), {id(session.projection_lock): session.projection_lock})
    call = ToolCall(name, args)
    result = ToolResult(call, f"success {URL}", ok=True, canonical={"opened_url": URL})
    with pytest.raises(ValueError, match="observation"):
        record_facts_for_result(session, call, result, ok=True)
    assert vars(session) == before


def test_redirect_and_query_facts_use_only_the_observed_identity():
    session = TaskSession(policy=None)
    search = ToolCall("web_search", {"query": "different requested query"})
    record_facts_for_result(session, search, ToolResult(search, "https://invented.example/x", ok=True,
                            canonical={"research_observation": search_receipt()}), ok=True)
    opened = ToolCall("open_url", {"url": "https://wrong.example/ignored"})
    observation = source_receipt()
    record_facts_for_result(session, opened, ToolResult(opened, "display text", ok=True,
                            canonical={"research_observation": observation, "source_id": "s1"}), ok=True)
    assert session.searches == ["observed query"]
    assert session.search_results == {"r1": FINAL}
    assert session.opened_sources == {FINAL}
    restored = ResearchLedger()
    restore_ledger_observation(restored, observation)
    assert restored.final_url_set() == {FINAL}
    assert restored.source_text_for_url(FINAL) == "exact observed content"
    assert restored.opened_sources[0].retrieved_at == observation["source"]["retrieved_at"]


def test_zero_search_results_are_a_valid_observation_not_a_missing_receipt():
    observation = search_receipt()
    observation["search"]["results"] = []
    session = TaskSession(policy=None)
    call = ToolCall("web_search", {"query": "q"})
    record_facts_for_result(session, call, ToolResult(call, "no results https://help.example/x", ok=True,
                            canonical={"research_observation": observation}), ok=True)
    ledger = ResearchLedger()
    restore_ledger_observation(ledger, observation)
    assert session.search_results == {}
    assert session.searches == ["observed query"]
    assert ledger.searches[0].results == ()


@pytest.mark.parametrize("name", ["web_search", "open_url", "open_result", "open_hit", "reopen_source"])
def test_failed_reads_never_publish_sources_even_with_success_shaped_payload(name):
    session = TaskSession(policy=None)
    call = ToolCall(name, {"url": URL})
    record_facts_for_result(session, call, ToolResult(call, f"ERROR: see {URL}", ok=False,
                            canonical={"research_observation": source_receipt()}), ok=False)
    assert not session.searches and not session.opened_sources and not session.source_ids


@pytest.mark.parametrize("field,value", [
    ("query", False), ("timestamp", "2026-10-24"), ("results", {}),
    ("results", [{"rank": 1, "title": "T", "url": False, "snippet": ""}]),
    ("results", [{"rank": True, "title": "T", "url": URL, "snippet": ""}]),
    ("results", [{"rank": 1, "title": "T", "url": "file:///secret", "snippet": ""}]),
])
def test_malformed_search_rejected_identically_without_partial_projection(field, value):
    observation = search_receipt()
    observation["search"][field] = value
    session, ledger = TaskSession(policy=None), ResearchLedger()
    call = ToolCall("web_search", {"query": "q"})
    with pytest.raises(ValueError):
        record_facts_for_result(session, call, ToolResult(call, URL, ok=True,
                                canonical={"research_observation": observation}), ok=True)
    with pytest.raises(ValueError):
        restore_ledger_observation(ledger, observation)
    assert session.searches == [] and session.search_results == {} and ledger.searches == []


@pytest.mark.parametrize("field,value", [
    ("final_url", False), ("requested_url", "file:///secret"), ("retrieved_at", "unknown"),
    ("page_count", True), ("pages_read", [True]), ("pages_read", [1, 1]),
    ("truncated", "false"), ("text_hash", "wrong"), ("quality", {}),
])
def test_malformed_source_rejected_identically_without_partial_projection(field, value):
    observation = source_receipt()
    observation["source"][field] = value
    _assert_source_rejected(observation)


@pytest.mark.parametrize("field,value", [("text", "tampered"), ("pages", None),
    ("pages", [{"number": 1, "text": "a"}, {"number": 1, "text": "b"}])])
def test_malformed_source_body_and_pages_are_not_accepted(field, value):
    observation = source_receipt()
    observation[field] = value
    _assert_source_rejected(observation)


def _assert_source_rejected(observation):
    session, ledger = TaskSession(policy=None), ResearchLedger()
    call = ToolCall("open_url", {"url": URL})
    with pytest.raises(ValueError):
        record_facts_for_result(session, call, ToolResult(call, URL, ok=True,
                                canonical={"research_observation": observation}), ok=True)
    with pytest.raises(ValueError):
        restore_ledger_observation(ledger, observation)
    assert session.opened_sources == set() and ledger.final_url_set() == set()


@pytest.mark.parametrize("name", ["web_search", "open_url", "source_search"])
def test_delegate_rejects_success_without_matching_observation(name):
    tools = SimpleNamespace(ledger=ResearchLedger(),
                            web_search=lambda *a: ResearchToolOutput(URL, ok=True),
                            source_search=lambda *a: ResearchToolOutput("1. offset 7: text", ok=True),
                            open_url=lambda *a, **kw: ResearchToolOutput("body", ok=True))
    result, ok, _ = ExecutionDelegate(session=TaskSession(policy=None), research_tools=tools)._execute_research(
        ToolCall(name, {"url": URL, "query": "q"}))
    assert ok is False and result.ok is False
    assert "observation" in result.model_text and not result.canonical


def test_empty_observation_is_not_a_valid_recovery_record():
    with pytest.raises(ValueError, match="observation"):
        restore_ledger_observation(ResearchLedger(), {})
