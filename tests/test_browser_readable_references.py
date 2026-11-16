"""Readable choices bind once, with original refs governing execution and replay."""
from __future__ import annotations

import json
from collections.abc import Iterator
from dataclasses import replace
from pathlib import Path

import pytest

from codey.automation.browser_session import BrowserSession
from codey.codemode.catalog import render_declarations, snapshot_catalog
from codey.codemode.kernel import build_execute_turn_executor
from codey.operations.browser_references import BrowserReferences
from codey.operations.browser_task_handoff import bind_browser_references
from codey.operations.completion_context import CompletionContext
from codey.operations.kernel_execution import execute_turn
from codey.operations.kernel_facts import project_execution_record
from codey.operations.kernel_protocol import (
    TurnSnapshot,
    build_turn_snapshot,
    normalize_turn,
    offer_browser_tools,
    refresh_snapshot_contract,
)
from codey.operations.provider_receipts import restore_snapshot, snapshot_description
from codey.operations.task_execution import ExecutionDelegate
from codey.operations.task_session import TaskSession
from codey.policies.tool_requirement import require_tool_success
from codey.protocols.text_turn import decode_text_turn
from codey.research.tools import ResearchTools
from codey.runtime.core.models import ToolCall, ToolResult
from codey.types.json_value import JsonObject, JsonValue, canonical_json_object, decode_json_object, require_json_string
from tests.test_browser_context_delivery import URL, install
from tests.test_browser_context_delivery import production_browser as production_browser
from tests.test_browser_observation_regressions import mutate
from tests.test_browser_result_adoption_ab import DOCUMENT, session_with_snapshot
from tests.test_receipt_bound_edit_surface_probe import install_http
from tools.browser_prerequisite_ab import code_action
from tools.browser_production_live_gate import run_case
from tools.completion_artifact_audit import tool_receipts
from tools.completion_explanation_ab import Target

TARGET = Target("http://scripted.test/v1", "scripted-fixture", 123456789, 15)


def binding(*, duplicate: bool = False, disabled: bool = False, same_context: bool = False) -> BrowserReferences:
    rows: list[JsonValue] = [{"ref": "long-original-ref-one", "role": "textbox", "name": "备注",
        "context_id": "c0", "disabled": False}]
    if duplicate:
        rows.append({"ref": "long-original-ref-two", "role": "textbox", "name": "备注",
                     "context_id": "c1", "disabled": disabled})
    value: JsonObject = {"generation": 7, "complete": True, "elements_truncated": False,
        "elements": rows, "contexts": [{"id": "c0", "label": "订单 A-173"},
            {"id": "c1", "label": "订单 A-173" if same_context else "订单 A-174"}]}
    return BrowserReferences.observe("source-snapshot", value, DOCUMENT)


def test_readable_binding_is_immutable_exact_and_roundtrips() -> None:
    original = binding()
    assert original.resolve({"role": "textbox", "name": "备注"}) == "long-original-ref-one"
    assert BrowserReferences.restore(original.payload()) == original
    detached = original.payload()
    controls = detached["controls"]
    assert isinstance(controls, list)
    controls.clear()
    assert len(original.controls) == 1
    with pytest.raises(ValueError, match="No matching"):
        original.resolve({"role": "textbox", "name": "备"})
    with pytest.raises(ValueError, match="Use a real"):
        original.resolve({"role": "textbox", "name": "备注", "invented": True})


@pytest.mark.parametrize("disabled", [False, True])
def test_uniqueness_precedes_availability(disabled: bool) -> None:
    original = binding(duplicate=True, disabled=disabled)
    with pytest.raises(ValueError, match="Ambiguous") as rejected:
        original.resolve({"role": "textbox", "name": "备注"})
    assert "long-original-ref-two" in str(rejected.value) and "A-174" in str(rejected.value)
    assert original.resolve({"role": "textbox", "name": "备注", "context": "订单 A-173"}) == "long-original-ref-one"
    if disabled:
        with pytest.raises(ValueError, match="disabled"):
            original.resolve({"role": "textbox", "name": "备注", "context": "订单 A-174"})


def test_duplicate_context_labels_still_require_a_real_string_ref() -> None:
    with pytest.raises(ValueError, match="Ambiguous"):
        binding(duplicate=True, same_context=True).resolve({"role": "textbox", "name": "备注", "context": "订单 A-173"})


@pytest.mark.parametrize("truncation", ["body", "controls", "receipt", "context", "generation", "document"])
def test_only_current_complete_controls_are_bound(truncation: str) -> None:
    session = session_with_snapshot()
    original = session._memory_results["snapshot"]
    value = decode_json_object(original.canonical["value"])
    value.update(text_truncated=truncation == "body", complete=truncation != "body")
    if truncation == "controls":
        value["elements_truncated"] = True
    if truncation == "context":
        empty_contexts: list[JsonValue] = []
        value["contexts"] = empty_contexts
    session._memory_results["snapshot"] = replace(original, canonical={"value": value}, truncated=truncation == "receipt")
    document = {**DOCUMENT, "loader_id": "new"} if truncation == "document" else DOCUMENT
    snapshot = offer_browser_tools(build_turn_snapshot(session), session)
    frozen = bind_browser_references(session, snapshot, CompletionContext(browser_document=document),
                                     8 if truncation == "generation" else 7)
    assert (frozen.browser_references is not None) is (truncation == "body")


@pytest.mark.parametrize("version", [6, 7, 8, 9])
def test_snapshot_versions_parse_separately_from_runtime_compatibility(version: int) -> None:
    session = session_with_snapshot()
    snapshot = offer_browser_tools(build_turn_snapshot(session), session) if version == 8 else build_turn_snapshot(session)
    payload = snapshot_description(snapshot)
    payload["schema_version"] = version
    if version < 9:
        del payload["browser_followups"], payload["browser_references"]
    if version < 8:
        del payload["browser_fill_text"]
    if version < 7:
        del payload["task_navigation_url"]
    assert restore_snapshot(payload) == snapshot
    payload["implementation_id"] = "previous-runtime"
    restored = restore_snapshot(payload)
    assert not restored.implementation_compatible
    assert restored.browser_references is None


def test_outer_and_nested_use_the_same_ref_schema_and_frozen_binding() -> None:
    session = session_with_snapshot()
    snapshot = offer_browser_tools(build_turn_snapshot(session), session)
    frozen = refresh_snapshot_contract(replace(snapshot, browser_references=binding()))
    assert restore_snapshot(snapshot_description(frozen)) == frozen
    outer = next(spec for spec in frozen.model_specs if spec.name == "browser_fill")
    nested = next(entry for entry in snapshot_catalog(frozen).entries if entry.name == "browser_fill")
    properties = decode_json_object(canonical_json_object(nested.parameters)["properties"])
    assert canonical_json_object(dict(outer.parameters)["ref"]) == properties["ref"]
    assert "anyOf" in decode_json_object(properties["ref"])
    choice: JsonObject = {"role": "textbox", "name": "备注", "context": "订单 A-173"}
    assert json.dumps(choice, ensure_ascii=False) in render_declarations(snapshot_catalog(frozen))
    action: JsonObject = {"tool": "browser_fill", "args": {"ref": {"role": "textbox", "name": "备注"}, "value": "x"}}
    assert not normalize_turn(decode_text_turn(json.dumps(action)), snapshot=frozen).protocol_error


def test_expired_binding_removes_descriptions_from_nested_declarations() -> None:
    session = session_with_snapshot()
    # Hybrid/CodeMode callers can have a control binding without Chat followups.
    snapshot = build_turn_snapshot(session)
    frozen = bind_browser_references(session, snapshot, CompletionContext(browser_document=DOCUMENT), 7)
    assert frozen.browser_references is not None and not frozen.browser_followups
    expired = bind_browser_references(session, frozen, CompletionContext(browser_document=DOCUMENT), 8)
    assert expired.browser_references is None
    codemode = next(spec for spec in expired.frozen_specs if spec.name == "codemode")
    assert codemode.declarations == render_declarations(snapshot_catalog(expired))


def observed_snapshot(session: TaskSession, browser: BrowserSession) -> TurnSnapshot:
    observed = browser.snapshot()
    receipt = ToolResult(ToolCall("browser_snapshot", {}), observed.model_text, ok=True,
        canonical={"value": observed.observation}, audit={"browser_document": browser.document_identity()})
    identity = f"snapshot-{browser.generation}"
    session._memory_results[identity] = receipt
    project_execution_record(session, identity, receipt.call, receipt, ok=True)
    snapshot = offer_browser_tools(build_turn_snapshot(session), session)
    return bind_browser_references(session, snapshot, CompletionContext(browser_document=browser.document_identity()), browser.generation)


def program(session: TaskSession, tools: ResearchTools, snapshot: TurnSnapshot, code: str,
            *, turn: int = 1, repair_of: str | None = None) -> ToolResult:
    parent = build_execute_turn_executor(session=session, snapshot=snapshot, run_id="readable-ref-run",
        effect_scope="task", turn=turn, research_tools=tools)
    args: JsonObject = {"code": code}
    if repair_of is not None:
        args["repair_of"] = repair_of
    return execute_turn(session, [ToolCall("codemode", args, f"parent-{turn}")], run_id="readable-ref-run",
        effect_scope="task", turn=turn, snapshot=snapshot, research_tools=tools, executors={"codemode": parent})[0]


@pytest.mark.parametrize("context", [None, "订单 A-173"])
def test_chromium_fill_never_resolves_ambiguity_by_discarding_a_disabled_neighbor(
    production_browser: tuple[TaskSession, ResearchTools, BrowserSession], context: str | None,
) -> None:
    session, tools, browser = production_browser
    assert ExecutionDelegate(session=session, research_tools=tools).execute(ToolCall("browser_navigate", {"url": URL}))[1]
    install(browser, '<section aria-label="订单 A-173"><input aria-label="备注"></section>'
        '<section aria-label="订单 A-174"><input aria-label="备注" disabled></section>')
    frozen = observed_snapshot(session, browser)
    selector: JsonObject = {"role": "textbox", "name": "备注"}
    if context is not None:
        selector["context"] = context
    result = program(session, tools, frozen, "return unwrap(await tools.browser_fill({ref:"
                     + json.dumps(selector, ensure_ascii=False) + ",value:'chosen'}));")
    assert result.ok is (context is not None), result.model_text
    fields = browser.evaluate("Array.from(document.querySelectorAll('input'),el=>el.value)").observation["value"]
    assert fields == (["chosen", ""] if context is not None else ["", ""])
    fills = [row for row in session._memory_results.values() if row.call.name == "browser_fill"]
    assert len(fills) == int(context is not None)


@pytest.mark.parametrize("change", ["name", "role", "disabled", "hidden", "replacement", "context", "generation", "page"])
def test_chromium_rejects_changed_meaning_before_delivery(
    production_browser: tuple[TaskSession, ResearchTools, BrowserSession], change: str,
) -> None:
    session, tools, browser = production_browser
    assert ExecutionDelegate(session=session, research_tools=tools).execute(ToolCall("browser_navigate", {"url": URL}))[1]
    install(browser, '<section aria-label="订单 A-173"><button onclick="window.delivered=true">Query</button></section>')
    frozen = observed_snapshot(session, browser)
    assert frozen.browser_references is not None
    mutations = {"name": "document.querySelector('button').textContent='Confirm submission'",
        "role": "document.querySelector('button').setAttribute('role','link')",
        "disabled": "document.querySelector('button').disabled=true",
        "hidden": "document.querySelector('button').hidden=true",
        "replacement": "document.querySelector('button').outerHTML='<button>Query</button>'",
        "context": "document.querySelector('section').setAttribute('aria-label','订单 A-174')"}
    if change == "generation":
        browser.snapshot()
    elif change == "page":
        assert browser.navigate(URL + "?new-document").ok
    else:
        mutate(browser, mutations[change])
    result = program(session, tools, restore_snapshot(snapshot_description(frozen)),
                     "return unwrap(await tools.browser_click({ref:{role:'button',name:'Query'}}));")
    assert not result.ok, result.model_text
    assert browser.evaluate("window.delivered===true").observation["value"] is False
    clicks = [row for row in session._memory_results.values() if row.call.name == "browser_click"]
    assert len(clicks) == 1 and decode_json_object(clicks[0].canonical["value"])["delivery"] == "not_delivered"
    assert decode_json_object(clicks[0].canonical["value"])["reason"] == (
        "disabled_control" if change == "disabled" else "stale_ref")
    assert isinstance(clicks[0].call.args["ref"], str)


@pytest.mark.parametrize("same_document", [False, True])
def test_repair_reuses_original_observation_after_the_current_page_changes(
    production_browser: tuple[TaskSession, ResearchTools, BrowserSession], same_document: bool,
) -> None:
    session, tools, browser = production_browser
    assert ExecutionDelegate(session=session, research_tools=tools).execute(ToolCall("browser_navigate", {"url": URL}))[1]
    install(browser, '<button onclick="window.delivered=(window.delivered||0)+1">Query</button>')
    frozen = observed_snapshot(session, browser)
    code = "const r=unwrap(await tools.browser_click({ref:{role:'button',name:'Query'}}));"
    failed = program(session, tools, frozen, code)
    assert not failed.ok and failed.audit.get("error_code") == "missing_return", failed.model_text
    token = require_json_string(decode_json_object(failed.canonical["repair"])["token"], "repair token")
    if same_document:
        mutate(browser, "document.querySelector('button').textContent='Other';document.body.insertAdjacentHTML('beforeend','<button>Query</button>')")
    else:
        assert browser.navigate(URL + "?another-document").ok
        install(browser, '<button onclick="window.wrong=true">Query</button>')
    current = observed_snapshot(session, browser)
    repaired = program(session, tools, current, code + "return r;", turn=2, repair_of=token)
    assert repaired.ok and repaired.audit["recovery_disposition"] == "receipt_replay", repaired.model_text
    assert browser.evaluate("window.delivered===1" if same_document else "window.wrong!==true").observation["value"] is True
    assert len([row for row in session._memory_results.values() if row.call.name == "browser_click"]) == 1


@pytest.mark.usefixtures("public_source_dns", "scripted_local_api_connection", "no_external_advisor_models")
@pytest.mark.parametrize("nested", [False, True])
def test_provider_guest_host_execute_descriptions_and_commit_only_string_refs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, nested: bool,
) -> None:
    directory = tmp_path / "descriptions"

    def replies() -> Iterator[JsonObject]:
        yield code_action("return unwrap(await tools.browser_snapshot({}));")
        fill: JsonObject = {"ref": {"role": "textbox", "name": "Locality"}, "value": "福州"}
        yield ({"tool": "codemode", "args": {"code": "unwrap(await tools.browser_fill(inputs));return unwrap(await tools.browser_snapshot({}));", "inputs": fill}}
               if nested else {"tool": "browser_fill", "args": fill})
        click: JsonObject = {"ref": {"role": "button", "name": "Load record"}, "purpose": "result"}
        yield ({"tool": "codemode", "args": {"code": "return unwrap(await tools.browser_click(inputs));", "inputs": click}}
               if nested else {"tool": "browser_click", "args": click})
        yield {"tool": "done", "args": {"summary": "实际净重 173 克，校验码 QUARTZ_851。"}}

    install_http(monkeypatch, replies())
    row = run_case(directory, "chat", TARGET, restore_snapshots=True, max_turns=4,
        additional_required_tools=(require_tool_success("browser_wait", arguments={"condition": "text", "value": "鱼丸"}),))
    assert row["passed"], row
    actions = [item.result for item in tool_receipts(directory / "state") if item.result.call.name in {"browser_fill", "browser_click"}]
    assert len(actions) == 2 and all(item.ok and isinstance(item.call.args["ref"], str) for item in actions)
    assert [item.result.call.name for item in tool_receipts(directory / "state")
            if item.result.call.name in {"browser_click", "browser_wait", "browser_read"}] == ["browser_click", "browser_wait", "browser_read"]
