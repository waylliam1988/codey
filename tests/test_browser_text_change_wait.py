"""Wait from a committed observation instead of replaying a delayed UI action."""
from __future__ import annotations

import threading
import time
from dataclasses import replace
from pathlib import Path

import pytest

from codey.automation.browser_session import BrowserSession, BrowserToolOutput
from codey.codemode.kernel import build_execute_turn_executor
from codey.durable.checkpoint import operation_from_checkpoint
from codey.knowledge.model import canonical_json
from codey.operations import browser_waits
from codey.operations.browser_resources import build_task_browser
from codey.operations.browser_task_handoff import bind_browser_references, browser_task_handoff
from codey.operations.completion_context import CompletionContext
from codey.operations.kernel_execution import execute_turn
from codey.operations.kernel_facts import project_execution_record
from codey.operations.kernel_progress import KernelProgress
from codey.operations.kernel_protocol import (
    build_turn_snapshot,
    normalize_turn,
    offer_browser_call,
    offer_browser_tools,
)
from codey.operations.progress_comparison import progress_comparison
from codey.operations.task_handoff import task_handoff
from codey.operations.task_session import TaskSession
from codey.operations.tool_tasks import run_tool_task
from codey.policies.task_policy import TaskPolicy
from codey.protocols.text_turn import decode_text_turn
from codey.providers.base import AssistantTurn, ProviderToolCall
from codey.research.tools import ResearchTools
from codey.runtime.core import cancellation
from codey.runtime.core.models import ToolCall, ToolResult
from codey.types.json_value import JsonObject, JsonValue, decode_json_object, parse_json_object, require_json_string
from tests.support.task_browser import install
from tests.test_browser_context_delivery import URL
from tests.test_browser_context_delivery import production_browser as production_browser
from tests.test_browser_readable_references import observed_snapshot, program
from tests.test_tool_task_background import open_runtime


def recorded_snapshot() -> tuple[TaskSession, JsonObject]:
    session = TaskSession(TaskPolicy(frozenset({"control", "web.read", "browser.observe", "browser.interact"})))
    document: JsonObject = {"url": URL, "target_id": "tab", "frame_id": "frame", "loader_id": "document"}
    value: JsonObject = {"text": "Working", "text_truncated": False, "document": document,
                         "scope": {"root_ref": "", "selector": ""}}
    result = ToolResult(ToolCall("browser_snapshot", {}), "Working", ok=True,
                        canonical={"value": value}, audit={"browser_document": document})
    session._memory_results["original-snapshot"] = result
    project_execution_record(session, "original-snapshot", result.call, result, ok=True)
    return session, document


@pytest.mark.parametrize("condition", ["valid", "uncommitted", "failed", "receipt_truncated", "text_truncated",
                                      "scoped", "wrong_document", "missing_text"])
def test_wait_baseline_requires_the_original_complete_committed_page_text(condition: str) -> None:
    session, document = recorded_snapshot()
    original = session._memory_results["original-snapshot"]
    value: JsonObject = decode_json_object(original.canonical["value"])
    if condition == "uncommitted":
        session.executed.clear()
    elif condition == "failed":
        original = replace(original, ok=False)
    elif condition == "receipt_truncated":
        original = replace(original, truncated=True)
    elif condition == "text_truncated":
        value["text_truncated"] = True
    elif condition == "scoped":
        value["scope"] = {"root_ref": "", "selector": "article"}
    elif condition == "wrong_document":
        value["document"] = {**document, "loader_id": "different"}
    elif condition == "missing_text":
        del value["text"]
    session._memory_results["original-snapshot"] = replace(original, canonical={"value": value})
    if condition == "valid":
        assert browser_waits.snapshot_text_baseline(session, "original-snapshot") == ("Working", document)
    else:
        with pytest.raises(ValueError, match="text_changed"):
            browser_waits.snapshot_text_baseline(session, "original-snapshot")


@pytest.mark.parametrize("condition", ["repeat", "intervening_action", "different_url", "uncertain"])
def test_navigation_repeat_feedback_uses_settled_effects_and_available_controls(condition: str) -> None:
    session, document = recorded_snapshot()
    original = session._memory_results.pop("original-snapshot")
    session.executed.clear()

    def commit(ref: str, result: ToolResult) -> None:
        session._memory_results[ref] = result
        project_execution_record(session, ref, result.call, result, ok=result.ok)

    first = ToolResult(ToolCall("browser_navigate", {"url": URL + "?other" if condition == "different_url" else URL}),
        "Navigation delivered", ok=condition != "uncertain", canonical={"value": {"delivery": "delivered"}},
        audit={"browser_document": {**document, "loader_id": "earlier"}})
    commit("first", first)
    if condition == "intervening_action":
        commit("input", ToolResult(ToolCall("browser_press", {"ref": "observed", "key": "Enter"}), "Delivered input",
            ok=True, canonical={"value": {"delivery": "delivered"}}, audit={"browser_document": first.audit["browser_document"]}))
    commit("second", ToolResult(ToolCall("browser_navigate", {"url": URL}), "Navigation delivered", ok=True,
        canonical={"value": {"delivery": "delivered"}}, audit={"browser_document": document}))
    value = decode_json_object(original.canonical["value"])
    value.update(generation=7, complete=True, elements_truncated=False, contexts=[], elements=[
        {"ref": "next", "role": "button", "name": "Advance", "context_id": "", "disabled": False}])
    commit("original-snapshot", replace(original, canonical={"value": value}))
    context = CompletionContext(browser_document=document)
    snapshot = bind_browser_references(session, offer_browser_tools(build_turn_snapshot(session), session), context, 7)
    _snapshot, hint = browser_task_handoff(session, snapshot, context, 7)
    assert ("Repeated navigation:" in hint) is (condition == "repeat")


def navigation_program(index: int, *, text: str = "Ready", value: str = "", parent_value: JsonObject | None = None,
                       ) -> tuple[ToolResult, dict[str, ToolResult]]:
    session = TaskSession(TaskPolicy(frozenset({"control", "web.read", "browser.observe", "browser.interact"})),
                          task_text=f"Open {URL}", required_source_urls=frozenset({URL}))
    snapshot = offer_browser_call(build_turn_snapshot(session), session, "browser_navigate")
    plan = normalize_turn(decode_text_turn('{"tool":"browser_navigate","args":{"target":"task"}}'), snapshot=snapshot)
    assert not plan.protocol_error
    document: JsonObject = {"url": URL, "target_id": "tab", "frame_id": "frame", "loader_id": f"loader-{index}"}
    navigation: JsonObject = {"action": "navigate", "url": URL, "previous_url": URL, "generation": index,
                              "page_id": "tab", "delivery": "delivered"}
    parent = f"navigation-{index}"
    first = ToolResult(ToolCall("browser_navigate", {"url": URL}, f"{parent}:child:0"), "Opened page", ok=True,
                       canonical={"value": navigation}, audit={"browser_document": document})
    observed: JsonObject = {"kind": "full", "complete": True, "generation": index + 1, "revision": index + 1,
        "text_truncated": False, "elements_truncated": False, "text": text, "document": document,
        "contexts": [{"id": f"region-{index}", "label": "Flow"}], "elements": [{"ref": f"node-{index}",
            "role": "textbox", "name": "Record", "disabled": False, "context_id": f"region-{index}", "value": value}]}
    second = ToolResult(ToolCall("browser_snapshot", {}, f"{parent}:child:1"), "Observed page", ok=True,
                        canonical={"value": observed}, audit={"browser_document": document})
    refs = [f"navigation-receipt-{index}", f"snapshot-receipt-{index}"]
    result = ToolResult(plan.calls[0], canonical_json(navigation), ok=True,
        canonical={"value": navigation if parent_value is None else parent_value, "child_effect_ids": list(refs)},
        audit={"codemode_execution_id": parent, "child_calls": 2})
    return result, dict(zip(refs, (first, second), strict=True))


def test_reload_transport_identity_is_not_new_business_information() -> None:
    first, receipts1 = navigation_program(1)
    second, receipts2 = navigation_program(2)
    original = canonical_json(second.canonical)
    before = progress_comparison(first, receipts1)
    after = progress_comparison(second, receipts2)
    assert after.call.name == "browser_navigate"
    assert before.information == after.information
    assert not after.browser_observation  # A delivered effect cannot become a safely stoppable read.
    assert canonical_json(second.canonical) == original


@pytest.mark.parametrize("cycle", [False, True])
def test_alternating_reload_and_snapshot_does_not_reset_the_progress_budget(cycle: bool) -> None:
    session = TaskSession(TaskPolicy(frozenset({"control", "web.read", "browser.observe", "browser.interact"})))
    progress = KernelProgress()
    for index in range(6):
        parent, receipts = navigation_program(index, text="Loading" if cycle and index % 2 else "Ready")
        session._memory_results.update(receipts)
        session._memory_results[f"parent-{index}"] = parent
        observed = receipts[f"snapshot-receipt-{index}"]
        explicit = replace(observed, call=ToolCall("browser_snapshot", {}, f"explicit-snapshot-{index}"))
        session._memory_results[f"explicit-{index}"] = explicit
        progress.observe([parent], session, batch_id=f"navigation-{index}")
        progress.observe([explicit], session, batch_id=f"observation-{index}")
    assert progress.idle >= 4
    assert progress.stopped  # A stalled reload cycle ends incomplete, never as business success.
    restored = KernelProgress(snapshot=progress.snapshot())
    assert restored.stopped and restored.idle == progress.idle


@pytest.mark.parametrize("change", ["body", "input", "unrelated_program", "other_document", "browser_click", "browser_press", "browser_select"])
def test_reload_snapshot_comparison_preserves_content_and_unproven_documents(change: str) -> None:
    comparisons: list[str] = []
    for index in range(2):
        parent, receipts = navigation_program(index, text="Changed" if index and change == "body" else "Ready",
            value="Input changed" if index and change == "input" else "")
        if change == "unrelated_program":
            parent = replace(parent, call=replace(parent.call, args={"code": "return inputs;", "inputs": {}}))
        receipts[f"parent-{index}"] = parent
        observed = receipts[f"snapshot-receipt-{index}"]
        if change.startswith("browser_"):
            receipts[f"action-{index}"] = ToolResult(ToolCall(change, {"ref": "control"}), "Delivered", ok=True,
                audit={"browser_document": observed.audit["browser_document"]})
        value = decode_json_object(observed.canonical["value"])
        if change == "other_document":
            document = {**decode_json_object(value["document"]), "loader_id": f"unrelated-{index}"}
            value["document"] = document
            observed = replace(observed, audit={"browser_document": document})
        explicit = replace(observed, call=ToolCall("browser_snapshot", {}), canonical={"value": value})
        receipts[f"explicit-{index}"] = explicit
        comparisons.append(progress_comparison(explicit, receipts).information)
    assert comparisons[0] != comparisons[1]


@pytest.mark.parametrize("change", ["body", "input", "computed_value", "extra_child"])
def test_navigation_comparison_preserves_visible_changes_and_arbitrary_program_values(change: str) -> None:
    first, receipts1 = navigation_program(1)
    second, receipts2 = navigation_program(2, text="Next control loaded" if change == "body" else "Ready",
        value="A-19" if change == "input" else "", parent_value={"business_generation": 2} if change == "computed_value" else None)
    if change == "extra_child":
        receipts2["undeclared"] = ToolResult(ToolCall("browser_click", {"ref": "other"}, "navigation-2:child:2"),
                                             "Additional effect", ok=True)
    assert progress_comparison(first, receipts1).information != progress_comparison(second, receipts2).information
    if change in {"computed_value", "extra_child"}:
        assert progress_comparison(second, receipts2).call.name == "codemode"


@pytest.mark.parametrize("transition", ["new_control", "enabled_control", "ref_churn", "still_loading", "text_progress",
                                       "wait_timeout", "wait_matched", "controls_timeout", "controls_matched",
                                       "controls_timeout_recaptured", "wait_timeout_recaptured",
                                       "controls_prior_timeout_recaptured", "controls_explicit_timeout_recaptured",
                                       "truncated_text", "declared_wait"])
def test_control_wait_is_not_offered_after_the_next_control_is_already_observed(transition: str) -> None:
    from codey.operations.browser_next_step import BrowserNextStep
    from codey.operations.browser_task_handoff import browser_control_wait_handoff

    session, document = recorded_snapshot()
    if transition == "controls_explicit_timeout_recaptured":
        session.task_text = "Click Begin once. Wait for the Continue button to become available."
    initial = session._memory_results["original-snapshot"]
    value = decode_json_object(initial.canonical["value"])
    begin: JsonObject = {"ref": "begin", "role": "button", "name": "Begin", "context_id": "", "disabled": False}
    next_control: JsonObject = {"ref": "continue", "role": "button", "name": "Continue", "context_id": "", "disabled": True}
    value.update(generation=1, complete=True, elements_truncated=False, contexts=[], elements=[begin])
    if transition == "enabled_control":
        value["elements"] = [begin, next_control]
    session._memory_results["original-snapshot"] = replace(initial, canonical={"value": value})
    click = ToolResult(ToolCall("browser_click", {"ref": "begin"}), "Delivered click", ok=True,
        canonical={"value": {"delivery": "delivered"}}, audit={"browser_document": document})
    session._memory_results["click"] = click
    project_execution_record(session, "click", click.call, click, ok=True)
    current: JsonObject = {**value, "generation": 2, "elements": [{**begin, "ref": "new-begin-ref"}]}
    if transition == "still_loading":
        current["text"] = "Loading the response."
    elif transition == "text_progress":
        current["text"] = "All requested actions completed."
    if transition in {"new_control", "enabled_control"}:
        current["elements"] = [begin, {**next_control, "disabled": False}]
    if transition == "truncated_text":
        current.update(complete=False, text_truncated=True)
    observation = replace(initial, canonical={"value": current})
    prior_baseline = transition == "controls_prior_timeout_recaptured"
    if not prior_baseline:
        session._memory_results["current"] = observation
        project_execution_record(session, "current", observation.call, observation, ok=True)
    generation = 2
    if transition.startswith(("wait_", "controls_")):
        condition = "controls_changed" if transition.startswith("controls_") else "text_changed"
        matched = transition.endswith("_matched")
        outcome: JsonObject = {"matched": matched} if matched else {"matched": False, "reason": "timeout"}
        baseline = "original-snapshot" if prior_baseline else "current"
        waited = ToolResult(ToolCall("browser_wait", {"condition": condition, "value": baseline, "timeout_ms": 5000}),
            "Wait settled", ok=matched, canonical={"value": outcome}, audit={"browser_document": document})
        session._memory_results["wait"] = waited
        project_execution_record(session, "wait", waited.call, waited, ok=waited.ok)
        if transition.endswith("_recaptured"):
            generation = 3
            recaptured = replace(initial, canonical={"value": {**current, "generation": generation}})
            session._memory_results["fresh"] = recaptured
            project_execution_record(session, "fresh", recaptured.call, recaptured, ok=True)
    context = CompletionContext(browser_document=document)
    snapshot = bind_browser_references(session, offer_browser_tools(build_turn_snapshot(session), session), context, generation)
    step = BrowserNextStep(ToolCall("browser_wait", {"condition": "text", "value": "Ready"}), "declared") if (
        transition == "declared_wait"
    ) else None
    _snapshot, hint = browser_control_wait_handoff(session, snapshot, context, generation, next_step=step)
    assert ('"condition": "text_changed"' in hint) is (transition == "ref_churn")
    assert ('"condition": "controls_changed"' in hint) is (transition in {"truncated_text", "controls_explicit_timeout_recaptured"})
    if transition == "declared_wait":
        assert hint == ""
    if transition in {"new_control", "enabled_control"}:
        assert "Continue" in hint and "already" in hint
    elif transition in {"wait_timeout", "wait_matched", "controls_timeout", "controls_matched"}:
        assert '"tool": "browser_snapshot"' in hint and "already settled" in hint
    elif transition == "controls_explicit_timeout_recaptured":
        assert '"tool": "browser_wait"' in hint and '"value": "fresh"' in hint
    elif transition.endswith("_recaptured"):
        assert "already settled" in hint and "Snapshot fresh" in hint
        assert '"tool": "browser_wait"' not in hint and '"tool": "browser_snapshot"' not in hint


@pytest.mark.parametrize("native", [False, True])
def test_delayed_native_controls_can_continue_without_repeating_the_delivered_click(
    production_browser: tuple[TaskSession, ResearchTools, BrowserSession], native: bool,
) -> None:
    session, tools, browser = production_browser
    assert browser.navigate(URL).ok
    install(browser, '''<main><p>Waiting for the next control.</p><button onclick="
        window.clicks=(window.clicks||0)+1;
        setTimeout(()=>{const next=document.createElement('button');next.textContent='Continue';
        next.onclick=()=>{window.clicks++;document.querySelector('p').textContent='Completed';};
        this.after(next);},1600)">Begin</button></main>''')
    snapshot = observed_snapshot(session, browser)
    clicked = program(session, tools, snapshot,
        "return unwrap(await tools.browser_click({ref:{role:'button',name:'Begin'},purpose:'action'}));")
    assert clicked.ok, clicked.model_text
    observed_snapshot(session, browser)
    current, hint = task_handoff(session, build_turn_snapshot(session), CompletionContext(), browser)
    assert current.browser_references is not None
    ref = current.browser_references.receipt_ref
    assert '"condition": "text_changed"' in hint and ref in hint
    args: JsonObject = {"condition": "text_changed", "value": ref, "timeout_ms": 4000}
    reply = (AssistantTurn(tool_calls=(ProviderToolCall("wait", "browser_wait", args),)) if native
             else decode_text_turn(canonical_json({"tool": "browser_wait", "args": args})))
    plan = normalize_turn(reply, snapshot=current)
    assert not plan.protocol_error, plan.protocol_error
    parent = build_execute_turn_executor(session=session, snapshot=current, run_id="readable-ref-run",
        effect_scope="task", turn=2, research_tools=tools)
    result = execute_turn(session, plan.calls, run_id="readable-ref-run", effect_scope="task", turn=2,
        snapshot=current, research_tools=tools, executors={"codemode": parent})[0]
    assert result.ok, result.model_text
    fresh, _hint = task_handoff(session, build_turn_snapshot(session), CompletionContext(), browser)
    assert fresh.browser_references is not None
    assert fresh.browser_references.receipt_ref != ref
    assert fresh.browser_references.resolve({"role": "button", "name": "Continue"})
    finished = program(session, tools, fresh,
        "return unwrap(await tools.browser_click({ref:{role:'button',name:'Continue'},purpose:'controls'}));", turn=3)
    assert finished.ok, finished.model_text
    assert browser.evaluate("window.clicks").observation["value"] == 2


def test_once_only_click_is_blocked_after_timeout_on_the_same_document(
    production_browser: tuple[TaskSession, ResearchTools, BrowserSession],
) -> None:
    session, tools, browser = production_browser
    session.task_text = "Click Begin once. Wait for the Continue button to become available."
    assert browser.navigate(URL).ok
    install(browser, '<button onclick="window.clicks=(window.clicks||0)+1">Begin</button>')
    body = "return await tools.browser_click({ref:{role:'button',name:'Begin'},purpose:'action'});"
    first = program(session, tools, observed_snapshot(session, browser), body)
    assert first.ok, first.model_text
    after_click = observed_snapshot(session, browser)
    assert after_click.browser_references is not None
    ref = after_click.browser_references.receipt_ref
    timed_out = program(session, tools, after_click,
        "return await tools.browser_wait({condition:'controls_changed',value:"
        + canonical_json(ref) + ",timeout_ms:100});", turn=2)
    assert "timed out" in timed_out.model_text
    repeat = program(session, tools, observed_snapshot(session, browser), body, turn=3)
    assert "already dispatched" in repeat.model_text
    assert browser.evaluate("window.clicks").observation["value"] == 1


@pytest.mark.parametrize("stop", ["timeout", "cancel", "deadline", "navigation"])
def test_observation_wait_preserves_timeout_cancellation_and_original_document(
    production_browser: tuple[TaskSession, ResearchTools, BrowserSession], stop: str,
) -> None:
    session, _tools, browser = production_browser
    assert browser.navigate(URL).ok
    install(browser, "<main>Stable text.</main>")
    snapshot = observed_snapshot(session, browser)
    assert snapshot.browser_references is not None
    text, document = browser_waits.snapshot_text_baseline(session, snapshot.browser_references.receipt_ref)
    if stop == "navigation":
        assert browser.navigate(URL + "?another-document").ok
        rejected = browser.wait_for_text_change(text, document, 500)
        assert not rejected.ok and "wait_document_changed" in rejected.model_text
    elif stop == "timeout":
        result = browser.wait_for_text_change(text, document, 150)
        assert not result.ok and result.observation["reason"] == "timeout"
    elif stop == "deadline":
        with cancellation.deadline_scope(time.monotonic() + 0.3):
            result = browser.wait_for_text_change(text, document, 5000)
        assert not result.ok and result.observation["reason"] in {"deadline", "cancelled"}
    else:
        stopped = threading.Event()
        timer = threading.Timer(0.3, stopped.set)
        timer.start()
        try:
            with cancellation.scope(stopped):
                result = browser.wait_for_text_change(text, document, 5000)
            assert not result.ok and result.observation["reason"] == "cancelled"
        finally:
            timer.cancel()
            timer.join()


@pytest.mark.parametrize("outcome", ["change", "managed_change", "worker_restart", "cancel", "navigation", "missing_snapshot"])
@pytest.mark.parametrize("condition", ["text_changed", "controls_changed"])
def test_background_text_wait_restores_the_original_snapshot_after_cache_loss(
    tmp_path: Path, production_browser: tuple[TaskSession, ResearchTools, BrowserSession],
    monkeypatch: pytest.MonkeyPatch, outcome: str, condition: str,
) -> None:
    session, tools, browser = production_browser
    runtime = open_runtime(tmp_path)
    build_task_browser("r", runtime.app.durable.tool_resources, tools, browser=browser)
    worker: threading.Thread | None = None
    snapshot = offer_browser_tools(build_turn_snapshot(session, native=True), session)

    def execute(call: ToolCall, turn: int) -> ToolResult:
        if turn:
            state = operation_from_checkpoint(runtime.app.durable.task("r"), "s")
            assert state is not None
            runtime.app.runtime_mutations.advance_execution("s", "r", expected=state, leaf="writer_running")
        return execute_turn(session, [call], run_id="r", turn=turn, session_id="s", snapshot=snapshot,
            durable=runtime.app.durable, intent_sink=runtime.sink, research_tools=tools,
            managed_outputs=runtime.app.managed_outputs)[0]

    try:
        assert browser.navigate(URL).ok
        install(browser, "<main><p>Waiting for the actual response.</p><button>Existing control</button>"
            + ("z" * 9000 if outcome == "managed_change" else "") + "</main>")
        observed = execute(ToolCall("browser_snapshot", {}), 0)
        assert observed.ok
        source_ref = next(ref for ref, result in session._memory_results.items() if result.call.name == "browser_snapshot")
        if outcome == "managed_change":
            settled = runtime.app.durable.effect(source_ref).settlement
            assert settled is not None and settled.get("result_ref")
        with pytest.raises(ValueError, match="owned by the parent"):
            browser_waits.restore_wait_snapshot(session, runtime.app.durable,
                owner_task_id="another-task", result_ref=source_ref, store=runtime.app.managed_outputs, condition="text_changed")
        checked = threading.Event()
        original = browser.wait_for_text_change
        original_controls = browser.wait_for_control_change

        def check(text: str, document: JsonObject, timeout_ms: int) -> BrowserToolOutput:
            checked.set()
            return original(text, document, timeout_ms)

        monkeypatch.setattr(browser, "wait_for_text_change", check)
        def check_controls(controls: list[JsonValue], document: JsonObject, timeout_ms: int) -> BrowserToolOutput:
            checked.set()
            return original_controls(controls, document, timeout_ms)

        monkeypatch.setattr(browser, "wait_for_control_change", check_controls)
        accepted = execute(ToolCall("browser_wait", {"condition": condition, "timeout_ms": 3000,
            "value": "not-a-snapshot" if outcome == "missing_snapshot" else source_ref, "run_in_background": True}), 1)
        if outcome == "missing_snapshot":
            assert not accepted.ok and condition in accepted.model_text
            assert not runtime.app.durable.owned_tasks("r")
            return
        assert accepted.ok and accepted.kind == "accepted", accepted.model_text
        child_id = require_json_string(parse_json_object(accepted.model_text)["task_ref"], "task_ref")
        child = runtime.app.durable.claim_task_checkpoint(child_id)
        session._memory_results.clear()
        session.executed.clear()
        browser.discard_references()
        binding = browser.resource_binding()
        if outcome == "worker_restart":
            browser._process.close(release=False)
        worker = threading.Thread(target=lambda: run_tool_task(runtime.app.durable, child,
            stores=runtime.app.task_submission_stores, signal=threading.Event()))
        worker.start()
        assert checked.wait(4), runtime.app.durable.task(child_id)
        if outcome in {"change", "managed_change", "worker_restart"}:
            assert browser.evaluate("document.querySelector('p').textContent='Actual response arrived';"
                "document.querySelector('main').insertAdjacentHTML('beforeend','<button>New control</button>')").ok
        elif outcome == "cancel":
            runtime.app.durable.request_cancel(child_id)
        else:
            assert browser.navigate(URL + "?replacement").ok
        worker.join(6)
        assert not worker.is_alive()
        finished = execute(ToolCall("task_output", {"task_ref": child_id}), 3)
        payload = parse_json_object(finished.model_text)
        if outcome in {"change", "managed_change", "worker_restart"}:
            assert payload["outcome"] == "succeeded", finished.model_text
        elif outcome == "cancel":
            assert payload["status"] == "cancelled", finished.model_text
        else:
            assert payload["outcome"] == "failed" and "wait_document_changed" in finished.model_text
        assert len(runtime.app.durable.effects(child_id)) == 1
        assert runtime.app.durable.effect(source_ref).task_id == "r"
        if outcome == "worker_restart":
            assert browser.resource_binding() == binding
    finally:
        if worker is not None and worker.is_alive():
            for child in runtime.app.durable.owned_tasks("r"):
                runtime.app.durable.request_cancel(child.task_id)
            worker.join(8)
        assert runtime.app.close()
