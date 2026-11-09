"""Acquisition observations never stand in for read evidence, including after restart."""
from __future__ import annotations

import copy
from pathlib import Path
from types import SimpleNamespace

import pytest

from codey.app.event_payloads import machine_event_payload
from codey.operations.kernel_facts import project_result_observation, record_facts_for_result
from codey.operations.task_session import TaskSession
from codey.policies.task_policy import TaskPolicy
from codey.research.ledger import ResearchLedger
from codey.research.source_acquisition import acquisition_observation, attempt, failure_event, with_attempt
from codey.research.web_sources import WebSourceProvider
from codey.runtime.core.models import ToolCall, ToolResult
from codey.runtime.observe.events import RunEvent, run_event_ui_payload
from codey.toolchain.runtime import ToolOutcome
from codey.toolchain.tool_spec import spec_for_tool
from codey.types.json_value import JsonObject, JsonValue, decode_json_object
from tests.support.receipt_fixtures import inspect_receipt_facts
from tests.support.session_projection import restore_task_session
from tests.test_chat_web_provider import Browser
from tests.test_committed_research_projection import runtime
from tests.test_source_intake_shared import StaticSource
from tests.test_source_version_reads import URL, turn


@pytest.mark.parametrize("code", ["access_restricted", "page_not_loaded", "render_script_error"])
def test_failure_receipt_event_and_restart_preserve_reason_without_refetch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, code: str,
) -> None:
    session, tools, managed, deps, sink = runtime(tmp_path, monkeypatch)
    calls: list[str] = []

    def http(url: str) -> dict[str, object]:
        calls.append(url)
        # Diagnostic deliberately contains a different category: UI must use the code.
        return with_attempt({"status": "error", "url": URL, "failure_code": code, "delivery": "confirmed",
                "detail": "unrelated diagnostic mentions HTTP 403 and page_blank"}, "http", url)

    monkeypatch.setattr("codey.research.web_sources.fetch_http_document", http)
    browser = Browser()
    monkeypatch.setattr(browser, "fetch", lambda url: {"status": "error", "url": url, "failure_code": code,
        "delivery": "confirmed", "detail": "unrelated diagnostic mentions HTTP 403 and page_blank"})
    tools.search = WebSourceProvider(browser_factory=lambda: browser)
    try:
        result = turn(session, tools, managed, sink, 1, ToolCall("open_url", {"url": URL}))
        assert not result.ok and not session.opened_sources and not tools.ledger.read_passages
        assert "research_observation" not in result.canonical and "read_passages" not in result.canonical
        raw = run_event_ui_payload("r", "s", RunEvent.tool_finished(1, result.call,
            ToolOutcome(result.model_text, False, canonical=result.canonical)))
        assert raw is not None
        event = machine_event_payload(raw)
        assert event is not None
        assert decode_json_object(event["failure"])["code"] == "source." + code
        expected = failure_event(result.canonical["source_failure"], run_id="r", session_id="s", call_id="1:0")
        assert raw["failure"] == expected
        recovered = inspect_receipt_facts(deps, session_id="s", run_id="r", project="", task_kind="chat")
        assert recovered.ok
        restored = TaskSession(session.policy)
        ledger = ResearchLedger()
        frame = SimpleNamespace(run_id="r", recovered_tool_outcomes=recovered.recovered_tool_outcomes,
                                settled_tool_outcomes=recovered.settled_tool_outcomes)
        restore_task_session(frame, restored, research_ledger=ledger)
        saved = next(iter(restored._memory_results.values()))
        assert saved.canonical == result.canonical and calls == [URL]
        assert not ledger.opened_sources and not ledger.read_passages
    finally:
        sink.durable.close()


@pytest.mark.parametrize("field,value", [("delivery", "unconfirmed"), ("observation", "read"),
                                         ("failure_code", "access_restricted"), ("status", True)])
def test_corrupt_attempt_is_rejected_before_projection(field: str, value: JsonValue) -> None:
    trace: JsonObject = {"version": 1, "attempts": [attempt("http", URL, {"status": "ok", "url": URL})]}
    entries = trace["attempts"]
    assert isinstance(entries, list)
    entry = decode_json_object(entries[0])
    # Test values are JSON primitives; no type washing at the production boundary.
    entry[field] = value
    trace["attempts"] = [entry]
    session = TaskSession(TaskPolicy(frozenset({"control", "web.read"})))
    result = ToolResult(ToolCall("open_url", {"url": URL}), "Navigation succeeded", ok=True,
                        canonical={"source_acquisition": trace})
    with pytest.raises(ValueError, match="acquisition|content requires"):
        project_result_observation(session, result.call, result, ok=True, research_ledger=ResearchLedger())
    assert not session.opened_sources


def test_navigation_or_acquisition_without_passages_is_not_source_read() -> None:
    session = TaskSession(TaskPolicy(frozenset({"control", "web.read"})))
    result = ToolResult(ToolCall("open_url", {"url": URL}), "HTTP 200", ok=True,
        canonical={"source_acquisition": {"version": 1, "attempts": [attempt("http", URL, {"status": "ok"})]}})
    with pytest.raises(ValueError, match="actual read passages"):
        project_result_observation(session, result.call, result, ok=True, research_ledger=ResearchLedger())
    assert not session.opened_sources


def test_search_target_cannot_change_version_or_offset_independently(monkeypatch: pytest.MonkeyPatch) -> None:
    from codey.operations.task_execution import ExecutionDelegate
    from codey.research.tools import ResearchTools

    monkeypatch.setattr("codey.research.source_gateway.check_fetch_url", lambda *a, **kw: None)
    session = TaskSession(TaskPolicy(frozenset({"control", "web.read"})))
    tools = ResearchTools(StaticSource("html"), None, None)
    assert tools.open_url(URL).ok
    call = ToolCall("source_search", {"url": URL, "query": "measured"})
    original, ok, _ = ExecutionDelegate(session=session, research_tools=tools)._execute_research(call)
    assert ok
    for key, changed in (("offset", 1), ("text_hash", "b" * 16)):
        canonical = copy.deepcopy(dict(original.canonical))
        canonical["hit_targets"]["h1"][key] = changed
        result = ToolResult(call, original.model_text, ok=True, canonical=canonical)
        with pytest.raises(ValueError, match="differ from the captured"):
            record_facts_for_result(session, call, result, ok=True)
        assert not session.hit_targets


def test_acquisition_tools_are_serial_and_never_automatically_replayed() -> None:
    for name in ("web_search", "open_url", "open_result", "source_search"):
        spec = spec_for_tool(name)
        assert spec is not None and spec.replay_class == "unsafe" and not spec.parallel_safe
    for name in ("open_hit", "reopen_source"):
        spec = spec_for_tool(name)
        assert spec is not None and spec.replay_class == "safe"


@pytest.mark.parametrize("name,args", [("open_url", {"url": URL}), ("web_search", {"query": "saffron"}),
                                      ("source_search", {"url": URL, "query": "saffron"})])
def test_interrupted_acquisition_retains_intent_without_reissuing_request(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, name: str, args: JsonObject,
) -> None:
    from codey.runtime.write.drive import peek_next_action

    session, tools, _managed, deps, sink = runtime(tmp_path, monkeypatch)
    try:
        sink.begin_turn([("pending-acquisition", ToolCall(name, args), 0)], turn=1)
        recovered = inspect_receipt_facts(deps, session_id="s", run_id="r", project="", task_kind="chat")
        assert recovered.ok and not recovered.recovered_tool_outcomes and not recovered.settled_tool_outcomes
        assert peek_next_action(sink.durable, session_id="s", run_id="r").kind == "synthesize_interrupted_effects"
        effects = sink.durable.effects("r")
        pending = next(row for row in effects if row.effect_id == "pending-acquisition")
        assert pending.replay_class == "unsafe" and pending.settlement is None
        assert not tools.search.calls and not session.opened_sources and not tools.ledger.read_passages
    finally:
        sink.durable.close()


@pytest.mark.parametrize("version", [True, 2, "1"])
def test_acquisition_protocol_is_exact_cold_start_v1(version: object) -> None:
    with pytest.raises(ValueError, match="version"):
        acquisition_observation({"version": version, "attempts": []})
