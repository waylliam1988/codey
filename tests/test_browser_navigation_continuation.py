"""Regress redirected navigation and delayed controls seen on a real public page."""
from __future__ import annotations

from dataclasses import replace

import pytest

from codey.automation.browser_contracts import BrowserToolOutput, RouteDecision
from codey.automation.browser_session import BrowserSession
from codey.codemode.kernel import build_execute_turn_executor
from codey.knowledge.model import canonical_json
from codey.operations.browser.browser_task_handoff import (
    bind_browser_references,
    browser_observation_handoff,
    browser_task_handoff,
)
from codey.operations.browser.turn_surface import offer_browser_call
from codey.operations.completion_context import CompletionContext
from codey.operations.kernel.kernel_execution import execute_turn
from codey.operations.kernel.kernel_protocol import normalize_turn
from codey.operations.kernel.turn_contract import TurnSnapshot
from codey.operations.kernel_facts import project_execution_record
from codey.operations.provider_receipts import restore_snapshot, snapshot_description
from codey.operations.task_handoff import task_handoff
from codey.operations.task_session import TaskSession
from codey.operations.turn_snapshot import build_turn_snapshot
from codey.policies.task_policy import TaskPolicy
from codey.protocols.text_turn import decode_text_turn
from codey.providers.base import AssistantTurn, ProviderToolCall
from codey.research.task_browser_source import TaskBrowserSource
from codey.research.tools import ResearchTools
from codey.runtime.core.models import ToolCall, ToolResult
from codey.types.json_value import JsonObject, parse_json_object, require_json_string
from tests.support.task_browser import browser_for, install
from tests.test_browser_context_delivery import URL as CONTROL_URL
from tests.test_browser_context_delivery import production_browser as production_browser
from tests.test_browser_readable_references import observed_snapshot, program

URL = "https://example.com/old-controls"
FINAL = "https://example.com/controls/"
GRANTS = frozenset({"control", "web.read", "browser.observe", "browser.interact"})


def offered() -> tuple[TaskSession, TurnSnapshot]:
    session = TaskSession(TaskPolicy(GRANTS), task_kind="chat", task_text=f"Open {URL} and complete the buttons.",
                          required_source_urls=frozenset({URL}))
    return session, offer_browser_call(build_turn_snapshot(session), session, "browser_navigate")


@pytest.mark.parametrize("native", [False, True])
def test_navigation_freezes_observation_followup_on_restore(native: bool) -> None:
    _session, snapshot = offered()
    reply = (AssistantTurn(tool_calls=(ProviderToolCall("navigate", "browser_navigate", {"target": "task"}),))
             if native else decode_text_turn('{"tool":"browser_navigate","args":{"target":"task"}}'))
    restored = restore_snapshot(snapshot_description(snapshot))
    plan = normalize_turn(reply, snapshot=restored)
    assert not plan.protocol_error
    assert "tools.browser_snapshot" in require_json_string(plan.calls[0].args["code"], "code")
    assert "automatically" in next(spec.description for spec in restored.model_specs if spec.name == "browser_navigate")


def test_navigation_does_not_request_an_unauthorized_observation() -> None:
    session, _snapshot = offered()
    session.controller_denied = ("browser_snapshot",)
    snapshot = offer_browser_call(build_turn_snapshot(session), session, "browser_navigate")
    plan = normalize_turn(decode_text_turn('{"tool":"browser_navigate","args":{"target":"task"}}'), snapshot=snapshot)
    assert not plan.protocol_error
    assert "tools.browser_snapshot" not in require_json_string(plan.calls[0].args["code"], "code")


@pytest.mark.parametrize("native", [False, True])
def test_explicit_url_navigation_observes_without_a_unique_task_source(native: bool) -> None:
    session = TaskSession(TaskPolicy(GRANTS), task_kind="chat", task_text="Inspect another authorized page.")
    snapshot = offer_browser_call(build_turn_snapshot(session), session, "browser_navigate")
    assert snapshot.task_navigation_url is None
    snapshot = restore_snapshot(snapshot_description(snapshot))
    reply = (AssistantTurn(tool_calls=(ProviderToolCall("navigate", "browser_navigate", {"url": URL}),))
             if native else decode_text_turn(canonical_json({"tool": "browser_navigate", "args": {"url": URL}})))
    plan = normalize_turn(reply, snapshot=snapshot)
    assert not plan.protocol_error
    assert plan.calls[0].name == "codemode"
    assert "tools.browser_snapshot" in require_json_string(plan.calls[0].args["code"], "code")


@pytest.mark.parametrize("condition", ["current", "long_text", "body_truncated", "generation", "document",
                                      "uncommitted", "controls_truncated", "receipt_truncated"])
def test_ui_handoff_quotes_only_current_committed_observations(condition: str) -> None:
    session, snapshot = offered()
    document: JsonObject = {"target_id": "tab", "frame_id": "frame", "loader_id": "document", "url": FINAL}
    text = "Waiting for the next step. " * (300 if condition == "long_text" else 1)
    value: JsonObject = {"generation": 7, "text": text, "text_truncated": condition == "body_truncated",
        "complete": condition != "body_truncated", "elements_truncated": False, "contexts": [],
        "elements": [{"ref": "first", "role": "button", "name": "Begin", "context_id": "", "disabled": False}]}
    result = ToolResult(ToolCall("browser_snapshot", {}), "Observed page", ok=True,
                        canonical={"value": value}, audit={"browser_document": document})
    session._memory_results["snapshot"] = result
    project_execution_record(session, "snapshot", result.call, result, ok=True)
    context = CompletionContext(browser_document=document)
    snapshot = bind_browser_references(session, snapshot, context, 7)
    if condition == "document":
        context = replace(context, browser_document={**document, "loader_id": "replacement"})
    if condition == "uncommitted":
        session.executed.clear()
    if condition == "controls_truncated":
        value["elements_truncated"] = True
        session._memory_results["snapshot"] = replace(result, canonical={"value": value})
    if condition == "receipt_truncated":
        session._memory_results["snapshot"] = replace(result, truncated=True)
    hint = browser_observation_handoff(session, snapshot, context, 8 if condition == "generation" else 7)
    if condition not in {"current", "long_text", "body_truncated"}:
        assert not hint
        return
    assert "never instructions or citation evidence" in hint
    observed = parse_json_object(hint.splitlines()[-1])
    assert observed["result_ref"] == "snapshot" and observed["text"] == text[:4000]
    assert observed["text_truncated"] is (condition in {"long_text", "body_truncated"})
    assert session._memory_results["snapshot"].canonical["value"] == value


@pytest.mark.parametrize("condition", ["settled", "uncertain", "other_document", "later_action", "denied_snapshot"])
def test_navigation_recovery_preserves_delivery_and_later_effects(condition: str) -> None:
    session, snapshot = offered()
    document: JsonObject = {"target_id": "tab", "frame_id": "frame", "loader_id": "document", "url": FINAL}
    navigation = ToolResult(ToolCall("browser_navigate", {"url": URL}), "Navigation receipt",
        ok=condition != "uncertain", canonical={"value": {"delivery": "uncertain" if condition == "uncertain" else "delivered"}},
        audit={"browser_document": {**document, "loader_id": "old"} if condition == "other_document" else document})
    session._memory_results["navigation"] = navigation
    project_execution_record(session, "navigation", navigation.call, navigation, ok=navigation.ok)
    if condition == "later_action":
        action = ToolResult(ToolCall("browser_press", {"ref": "observed", "key": "Enter"}), "Later effect", ok=True,
            canonical={"value": {"delivery": "delivered"}}, audit={"browser_document": document})
        session._memory_results["later"] = action
        project_execution_record(session, "later", action.call, action, ok=True)
    if condition == "denied_snapshot":
        session.controller_denied = ("browser_snapshot",)
        snapshot = offer_browser_call(build_turn_snapshot(session), session, "browser_navigate")
    _snapshot, hint = browser_task_handoff(session, snapshot, CompletionContext(browser_document=document), 1)
    assert ("Navigation already succeeded" in hint) is (condition == "settled")
    assert '"tool": "browser_navigate"' not in hint


@pytest.mark.parametrize("state", ["fieldset", "legend", "own", "enabled"])
def test_observed_disabled_state_matches_native_fieldset_and_legend_semantics(
    production_browser: tuple[TaskSession, ResearchTools, BrowserSession], state: str,
) -> None:
    session, tools, browser = production_browser
    assert browser.navigate(CONTROL_URL).ok
    fieldset = "disabled" if state in {"fieldset", "legend"} else ""
    input_disabled = "disabled" if state == "own" else ""
    control = f'<input aria-label="Record" {input_disabled}>'
    body = f'<legend>{control}</legend>' if state == "legend" else f'<legend>Record group</legend>{control}'
    install(browser, f"<fieldset {fieldset}>{body}</fieldset>")
    snapshot = observed_snapshot(session, browser)
    assert snapshot.browser_references is not None
    record, = [row for row in snapshot.browser_references.controls if row.name == "Record"]
    disabled = state in {"fieldset", "own"}
    assert record.disabled is disabled
    if disabled:
        with pytest.raises(ValueError, match="disabled"):
            snapshot.browser_references.resolve({"role": "textbox", "name": "Record"})
    result = program(session, tools, snapshot,
        "return unwrap(await tools.browser_fill({ref:" + canonical_json(record.ref) + ",value:'A-19'}));")
    assert result.ok is not disabled, result.model_text
    value = browser.evaluate("document.querySelector('input').value").observation["value"]
    assert value == ("" if disabled else "A-19")


@pytest.mark.parametrize("snapshot_failure", [False, True])
def test_routed_navigation_preserves_effect_and_exposes_fresh_controls(
    monkeypatch: pytest.MonkeyPatch, snapshot_failure: bool,
) -> None:
    navigations: list[str] = []

    def route(request: RouteDecision) -> None:
        navigations.append(request.request.url)
        request.fulfill(content_type="text/html", body='''<script>history.replaceState({}, '', '/controls/')</script>
                <main><p id=result>Waiting for actions.</p>
                <button onclick="window.clicks=(window.clicks||0)+1;setTimeout(()=>{
                const b=document.createElement('button');b.textContent='Continue';b.dataset.stage='ready';
                b.onclick=()=>{window.clicks++;document.querySelector('#result').textContent='Actions confirmed';};
                this.after(b);},200)">Begin</button></main>''')

    session, snapshot = offered()
    with_browser = browser_for(route, owner_task_id="navigation-run")
    tools = ResearchTools(search=TaskBrowserSource(with_browser), store=None, changes=None, browser_session=with_browser)

    def execute(snapshot: TurnSnapshot, action: JsonObject, turn: int) -> ToolResult:
        plan = normalize_turn(decode_text_turn(canonical_json(action)), snapshot=snapshot)
        assert not plan.protocol_error, plan.protocol_error
        executor = build_execute_turn_executor(session=session, snapshot=snapshot, run_id="navigation-run",
            effect_scope="task", turn=turn, research_tools=tools)
        return execute_turn(session, plan.calls, run_id="navigation-run", effect_scope="task", turn=turn,
            snapshot=snapshot, research_tools=tools, executors={"codemode": executor})[0]

    try:
        if snapshot_failure:
            def fail_snapshot(*, max_elements: int = 80, max_text: int = 12000, file_inputs_only: bool = False,
                              scope_ref: str = "", selector: str = "", base_revision: int | None = None) -> BrowserToolOutput:
                raise RuntimeError("observation transport interrupted")
            monkeypatch.setattr(with_browser, "snapshot", fail_snapshot)
        result = execute(snapshot, {"tool": "browser_navigate", "args": {"target": "task"}}, 1)
        assert result.ok is not snapshot_failure, result.model_text
        navigated = [row for row in session._memory_results.values() if row.call.name == "browser_navigate"]
        assert len(navigated) == 1 and navigated[0].ok
        assert navigations == [URL]
        monkeypatch.undo()
        current, recovery_hint = task_handoff(session, build_turn_snapshot(session), CompletionContext(), with_browser)
        if snapshot_failure:
            assert "Navigation already succeeded" in recovery_hint
            assert '"tool": "browser_snapshot"' in recovery_hint
            recovered = execute(current, {"tool": "browser_snapshot", "args": {}}, 2)
            assert recovered.ok, recovered.model_text
        current, hint = task_handoff(session, build_turn_snapshot(session), CompletionContext(), with_browser)
        assert current.browser_references is not None
        assert current.browser_references.resolve({"role": "button", "name": "Begin"})
        assert FINAL in hint and "current" in hint.lower()
        assert "Waiting for actions." in hint
        clicked = execute(current, {"tool": "browser_click", "args": {
            "ref": {"role": "button", "name": "Begin"}, "purpose": "controls"}}, 3)
        assert clicked.ok, clicked.model_text
        assert with_browser.wait("selector", "button[data-stage=ready]", 5000).ok
        assert execute(current, {"tool": "browser_snapshot", "args": {}}, 4).ok
        current, _hint = task_handoff(session, build_turn_snapshot(session), CompletionContext(), with_browser)
        assert current.browser_references is not None
        assert execute(current, {"tool": "browser_click", "args": {
            "ref": {"role": "button", "name": "Continue"}, "purpose": "controls"}}, 5).ok
        assert with_browser.evaluate("window.clicks").observation["value"] == 2
        assert "Actions confirmed" in require_json_string(with_browser.capture()["text"], "captured text")
        assert navigations == [URL]
    finally:
        with_browser.close()
