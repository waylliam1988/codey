"""Task navigation recovery requires a current, observed HTTP 404 without controls."""
from __future__ import annotations

import json
from collections.abc import Iterator
from dataclasses import replace
from pathlib import Path

import pytest

from codey.automation.browser_contracts import RouteDecision as Route
from codey.automation.browser_document import same_document
from codey.automation.browser_session import BrowserSession
from codey.operations.browser.browser_task_handoff import _call_handoff, browser_task_handoff
from codey.operations.completion_context import CompletionContext
from codey.operations.provider_receipts import restore_snapshot, snapshot_description
from codey.operations.task_execution import ExecutionDelegate
from codey.operations.task_session import TaskSession
from codey.operations.turn_snapshot import build_turn_snapshot
from codey.research.tools import ResearchTools
from codey.runtime.core.models import ToolCall, ToolResult
from codey.types.json_value import JsonObject, decode_json_object, require_json_string
from tests.test_browser_context_delivery import production_browser as production_browser
from tests.test_browser_failure_handoff import DOCUMENT, bound_session, next_step, remember
from tests.test_browser_failure_handoff import handoffs as handoffs
from tests.test_browser_observation_regressions import mutate
from tests.test_task_contract_fixed_replies import TARGET, install_original_http, saved_replies
from tools.browser_prerequisite_ab import LOAD, READ, WAIT, code_action
from tools.browser_production_live_gate import URL, run_case
from tools.completion_artifact_audit import tool_receipts

WRONG = URL.replace("production-browser-record", "production_browser_record")


def wrong_page() -> tuple[TaskSession, JsonObject]:
    session = bound_session()
    document: JsonObject = {**DOCUMENT, "loader_id": "wrong-document", "url": WRONG}
    remember(session, "wrong-navigation", ToolResult(ToolCall("browser_navigate", {"url": WRONG}, "model-navigation"),
        "Opened", ok=True, audit={"browser_document": document}, canonical={"value": {"url": WRONG, "http_status": 404}}))
    remember(session, "snapshot", ToolResult(ToolCall("browser_snapshot", {}, "snapshot"), "Observed", ok=True,
        audit={"browser_document": document}, canonical={"value": {
            "generation": 2, "complete": True, "elements_truncated": False, "elements": []}}))
    return session, document


@pytest.mark.parametrize("parent_ok", [False, True])
def test_successful_navigation_to_404_and_empty_snapshot_offer_the_bound_selector(parent_ok: bool) -> None:
    session, document = wrong_page()
    parent = ToolResult(ToolCall("codemode", {}, "parent"), "Returned", ok=parent_ok,
        canonical={"child_effect_ids": ["wrong-navigation", "snapshot"]})
    remember(session, "parent", parent)
    snapshot = restore_snapshot(snapshot_description(build_turn_snapshot(session)))
    offered, hint = browser_task_handoff(session, snapshot, CompletionContext(browser_document=document), 2,
                                         delivered_results=[parent])
    assert session._memory_results["wrong-navigation"].ok
    assert next_step(hint) == {"tool": "browser_navigate", "args": {"target": "task"}}
    assert offered.task_navigation_url == URL


@pytest.mark.parametrize("state", ["200", "no_status", "failed_navigation", "no_snapshot", "controls", "incomplete",
    "elements_truncated", "receipt_truncated", "old_generation", "other_document", "missing_document", "redirect",
    "final_task_url", "push_state", "required_source", "uncommitted", "snapshot_before_navigation", "denied",
    "ambiguous", "click", "uncertain_upload", "download", "fill", "edit", "unconfirmed_edit"])
def test_navigation_recovery_does_not_infer_failure_or_discard_actions(state: str) -> None:
    session, document = wrong_page()
    navigation = session._memory_results["wrong-navigation"]
    snapshot = session._memory_results["snapshot"]
    nav_value = decode_json_object(navigation.canonical["value"])
    value = decode_json_object(snapshot.canonical["value"])
    generation = 2
    current_document: JsonObject | None = document
    if state in {"200", "no_status"}:
        nav_value["http_status"] = 200 if state == "200" else None
    elif state == "failed_navigation":
        navigation = replace(navigation, ok=False)
    elif state == "no_snapshot":
        session.executed.pop("snapshot")
    elif state == "controls":
        value["elements"] = [{"name": "Continue", "ref": "real-control"}]
    elif state in {"incomplete", "elements_truncated"}:
        value["complete" if state == "incomplete" else "elements_truncated"] = state == "elements_truncated"
    elif state == "receipt_truncated":
        snapshot = replace(snapshot, truncated=True)
    elif state == "old_generation":
        generation = 3
    elif state == "other_document":
        navigation = replace(navigation, audit={"browser_document": DOCUMENT})
    elif state == "missing_document":
        current_document = None
    elif state == "redirect":
        navigation = replace(navigation, call=ToolCall("browser_navigate", {"url": URL}, "model-navigation"))
    elif state == "final_task_url":
        nav_value["url"] = URL
        current_document = {**document, "url": URL}
        snapshot = replace(snapshot, audit={"browser_document": current_document})
    elif state == "push_state":
        current_document = {**document, "url": WRONG + "?state=2"}
        snapshot = replace(snapshot, audit={"browser_document": current_document})
    elif state == "required_source":
        navigation = replace(navigation, call=replace(navigation.call, call_id="required-source:navigation"))
    elif state == "uncommitted":
        session.executed.pop("wrong-navigation")
    elif state == "snapshot_before_navigation":
        session.executed = {"snapshot": session.executed["snapshot"], **{
            ref: row for ref, row in session.executed.items() if ref != "snapshot"}}
    elif state == "denied":
        session.controller_denied = ("browser_navigate",)
    elif state == "ambiguous":
        session.task_text += " Also open https://example.com/another-page"
        session.required_source_urls = frozenset({URL, "https://example.com/another-page"})
    elif state in {"click", "uncertain_upload", "download", "fill", "edit", "unconfirmed_edit"}:
        name = ("browser_upload" if state == "uncertain_upload" else "edit" if state in {"edit", "unconfirmed_edit"}
                else "browser_" + state)
        canonical: JsonObject = ({"workspace_unconfirmed": True, "identity_reason": "scan failed"}
            if state == "unconfirmed_edit" else {"value": {
                "delivery": "uncertain" if state == "uncertain_upload" else "delivered"}})
        remember(session, "action", ToolResult(ToolCall(name, {}, "action"), "Sent",
            ok=state not in {"uncertain_upload", "unconfirmed_edit"}, canonical=canonical,
            audit={"browser_document": document}))
    navigation = replace(navigation, canonical={"value": nav_value})
    snapshot = replace(snapshot, canonical={"value": value})
    for ref, receipt in (("wrong-navigation", navigation), ("snapshot", snapshot)):
        if ref in session.executed:
            remember(session, ref, receipt)
        else:
            session._memory_results[ref] = receipt
    _, hint = browser_task_handoff(session, build_turn_snapshot(session),
                                  CompletionContext(browser_document=current_document), generation)
    assert '"target": "task"' not in hint


@pytest.mark.parametrize("args", [{"target": "task"}, {"target": "other"}, {"target": "task", "url": WRONG},
                                    {"target": "task", "extra": True}])
@pytest.mark.parametrize("bound", [False, True])
def test_selector_handoff_validates_the_existing_frozen_codemode_catalog(args: JsonObject, bound: bool) -> None:
    session = bound_session()
    if not bound:
        session.task_text = "Observe the current page"
        session.required_source_urls = frozenset()
    _, hint = _call_handoff(session, build_turn_snapshot(session), ToolCall("browser_navigate", args))
    assert ("Execute this next step:" in hint) is (bound and args == {"target": "task"})


@pytest.mark.usefixtures("public_source_dns", "scripted_local_api_connection", "no_external_advisor_models")
@pytest.mark.parametrize("parent_ok", [False, True])
def test_saved_wrong_url_reply_returns_through_the_offered_task_binding(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, handoffs: list[str], parent_ok: bool,
) -> None:
    directory = tmp_path / "navigation"
    raw = (json.dumps(code_action("unwrap(await tools.browser_navigate({url:'" + WRONG + "'}));"
                                 "return unwrap(await tools.browser_snapshot({}));")) if parent_ok else saved_replies("basic")[1])

    def replies() -> Iterator[str]:
        yield raw
        yield json.dumps(next_step(handoffs[-1]))
        yield json.dumps(code_action("return unwrap(await tools.browser_snapshot({}));"))
        snapshots = [r.result for r in tool_receipts(directory / "state") if r.result.call.name == "browser_snapshot"]
        empty, recovered = snapshots[0], snapshots[-1]
        assert decode_json_object(empty.audit["browser_document"])["url"] == WRONG
        assert decode_json_object(recovered.audit["browser_document"])["url"] == URL
        assert decode_json_object(empty.canonical["value"])["elements"] == []
        controls = decode_json_object(recovered.canonical["value"])["elements"]
        assert isinstance(controls, list) and any(isinstance(item, dict) and item.get("name") == "Locality"
                                                for item in controls)
        yield json.dumps(code_action(LOAD + WAIT + f"return {READ};"))
        completed: JsonObject = {"tool": "done", "args": {"summary": "净重 173 克，校验码 QUARTZ_851。"}}
        yield json.dumps(completed)

    install_original_http(monkeypatch, replies())
    row = run_case(directory, "chat", TARGET, max_turns=5)
    assert row["passed"], row
    receipts = tool_receipts(directory / "state")
    navigations = [r.result for r in receipts if r.result.call.name == "browser_navigate"]
    initial, wrong, restored = navigations
    assert initial.call.call_id == "required-source:navigation" and initial.call.args == {"url": URL}
    assert wrong.ok and wrong.call.args == {"url": WRONG}
    assert decode_json_object(wrong.canonical["value"])["http_status"] == 404
    assert restored.call.args == {"url": URL} and restored.ok
    parents = [r.result for r in receipts if r.result.call.name == "codemode"]
    assert parents[0].ok is parent_ok
    assert decode_json_object(parents[1].call.args["inputs"]) == {"target": "task"}


@pytest.mark.usefixtures("public_source_dns")
@pytest.mark.parametrize("flow", ["second_page", "redirect", "push_state"])
def test_real_browser_normal_transitions_do_not_offer_task_recovery(
    production_browser: tuple[TaskSession, ResearchTools, BrowserSession], monkeypatch: pytest.MonkeyPatch, flow: str,
) -> None:
    session, tools, browser = production_browser
    session.task_text = f"Open <{URL}> and follow the page's authorized flow."
    session.required_source_urls = frozenset({URL})

    def route_document(route: Route) -> None:
        if flow == "redirect" and route.request.url == URL:
            route.fulfill(status=302, headers={"location": URL + "/login"}, body="")
        else:
            route.fulfill(status=404 if flow in {"redirect", "push_state"} else 200,
                          content_type="text/html", body="<article>Current page</article>")

    from codey.automation.browser_routes import route_policy

    monkeypatch.setattr(browser._process, "network_policy", route_policy(route_document))
    delegate = ExecutionDelegate(session=session, research_tools=tools)
    navigation, ok, _ = delegate.execute(ToolCall("browser_navigate", {"url": URL}, "first-navigation"))
    assert ok
    remember(session, "first-navigation", navigation)
    if flow in {"second_page", "push_state"}:
        navigation, ok, _ = delegate.execute(ToolCall("browser_navigate", {"url": WRONG}, "second-navigation"))
        assert ok
        remember(session, "second-navigation", navigation)
    if flow == "push_state":
        mutate(browser, "history.pushState({},'', '?valid-flow=1')")
        assert same_document(navigation.audit["browser_document"], browser.document_identity())
    observed, ok, _ = delegate.execute(ToolCall("browser_snapshot", {}, "snapshot"))
    assert ok
    remember(session, "snapshot", observed)
    current = browser.document_identity()
    assert current is not None
    value = decode_json_object(observed.canonical["value"])
    assert value["complete"] is True and value["elements"] == []
    assert require_json_string(current["url"], "URL") != URL
    _, hint = browser_task_handoff(session, build_turn_snapshot(session), CompletionContext(browser_document=current),
                                  browser.generation)
    assert '"target": "task"' not in hint
