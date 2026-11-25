"""Shared click contract and mechanical RPC sequence, including preflight failures."""
from __future__ import annotations

import json
from collections.abc import Callable, Sequence
from dataclasses import replace

import pytest

from codey.codemode.catalog import build_codemode_catalog, render_declarations
from codey.codemode.host import CodeModeHost
from codey.codemode.runtime import NodeWorkerRuntime
from codey.codemode.types import ToolCatalogEntry
from codey.operations.browser.browser_waits import observed_link_outcome, pending_link_click
from codey.operations.browser.catalog import browser_catalog_specs, snapshot_catalog
from codey.operations.browser.turn_surface import offer_browser_tools
from codey.operations.kernel_facts import project_execution_record
from codey.operations.kernel_prompt import _browser_followup_context, _browser_read_context
from codey.operations.provider_receipts import restore_snapshot, snapshot_description
from codey.operations.task_session import TaskSession
from codey.operations.turn_snapshot import build_turn_snapshot
from codey.policies.task_policy import TaskPolicy
from codey.policies.tool_requirement import require_tool_success
from codey.protocols.codemode import CodeModeLimits
from codey.runtime.core.models import ToolCall, ToolResult
from codey.types.json_value import JsonObject, JsonValue, canonical_json_object, decode_json_object, parse_json_object

GRANTS = frozenset({"control", "web.read", "browser.observe", "browser.interact"})


@pytest.mark.parametrize("kind", ["snapshot", "read", "failed_read", "later_navigation"])
def test_followup_presentation_uses_settled_receipts_without_source_marker(kind: str) -> None:
    session = TaskSession(TaskPolicy(GRANTS), task_kind="chat")
    click = ToolResult(ToolCall("browser_click", {"ref": "observed"}, call_id="exec:child:0"),
                       "clicked", ok=True, audit={"query_baseline": {}}, canonical={"value": {"delivery": "delivered"}})
    name = "browser_snapshot" if kind in {"snapshot", "later_navigation"} else "browser_read"
    observation = ToolResult(ToolCall(name, {}, call_id="exec:child:1"), "OBSERVED_CONTENT", ok=kind != "failed_read",
                             audit={"query_click": "click"} if name == "browser_read" else {})
    refs = ["click", "observation"]
    session._memory_results.update(click=click, observation=observation)
    if kind == "later_navigation":
        refs.append("navigation")
        session._memory_results["navigation"] = ToolResult(
            ToolCall("browser_navigate", {"url": "https://example.com/other"}, call_id="exec:child:2"), "navigated", ok=True)
    parent = ToolResult(ToolCall("codemode", {}), "parent", ok=kind != "failed_read",
                        audit={"codemode_execution_id": "exec"}, canonical={"child_effect_ids": refs})
    session._memory_results["parent"] = parent
    text = _browser_followup_context(parent, session)
    read_text = _browser_read_context(parent, session, receipt_recovery=True)
    if kind == "snapshot":
        assert "OBSERVED_CONTENT" in text
    elif kind == "read":
        assert "OBSERVED_CONTENT" in read_text and "unconfirmed" not in text
    elif kind == "failed_read":
        assert "without repeating the click" in text
    else:
        assert "OBSERVED_CONTENT" not in text


@pytest.mark.parametrize("purpose,expected", [
    ("action", ["browser_click"]), ("controls", ["browser_click", "browser_snapshot", "browser_read"]),
    ("result", ["browser_click", "browser_wait", "browser_read"]),
])
def test_frozen_contract_dispatches_each_leaf_once(purpose: str, expected: list[str]) -> None:
    wait: JsonObject = {"condition": "text", "value": "Ready"}
    session = TaskSession(TaskPolicy(GRANTS, required_checks=(require_tool_success("browser_wait", arguments=wait),)),
                          task_kind="chat")
    snapshot = offer_browser_tools(build_turn_snapshot(session), session)
    catalog = snapshot_catalog(snapshot)
    snapshot = restore_snapshot(parse_json_object(json.dumps(snapshot_description(snapshot), sort_keys=True)))
    assert snapshot_catalog(snapshot).digest == catalog.digest
    assert next(s.declarations for s in snapshot.model_specs if s.name == "codemode") == render_declarations(
        catalog, outer_descriptions={spec.name: spec.description for spec in snapshot.model_specs})
    calls: list[tuple[str, JsonObject]] = []

    def invoke(name: str, args: JsonObject) -> ToolResult:
        calls.append((name, decode_json_object(args)))
        return ToolResult(ok=True, call=ToolCall(name, decode_json_object(args)), model_text="leaf-result",
                          canonical={"value": _valid_browser_value(name)})

    result = NodeWorkerRuntime().execute("return await tools.browser_click(inputs);", catalog=catalog.entries,
        inputs={"ref": "observed", "purpose": purpose},
        invoke=_browser_host(catalog.entries, invoke).invoke)
    assert result.ok, result.error
    assert [name for name, _ in calls] == expected
    assert result.child_calls == len(expected)
    assert result.value == {"ok": True, "value": _valid_browser_value("browser_click")}
    if purpose == "result":
        assert calls[1][1] == {**wait, "timeout_ms": 5000}


@pytest.mark.parametrize("failure", ["purpose", "extra", "read_denied", "unknown_wait", "budget"])
def test_unavailable_plan_rejects_before_click(failure: str) -> None:
    session = TaskSession(TaskPolicy(GRANTS), task_kind="chat")
    specs = list(build_turn_snapshot(session).frozen_specs)
    checks = (require_tool_success("browser_wait"),) if failure == "unknown_wait" else ()
    if failure == "read_denied":
        specs = [spec for spec in specs if spec.name != "browser_read"]
    browser_specs, plans = browser_catalog_specs(tuple(specs), checks, observe_links=False)
    catalog = build_codemode_catalog(browser_specs, click_followups=plans)
    args: JsonObject = {"ref": "observed", "purpose": "wrong" if failure == "purpose" else "result"}
    if failure == "extra":
        args["unexpected"] = True
    calls: list[str] = []

    def invoke(name: str, _args: JsonObject) -> ToolResult:
        calls.append(name)
        call = ToolCall(name, {})
        return ToolResult(ok=True, call=call, model_text="unexpected", canonical={"value": _valid_browser_value(name)})

    limits = replace(CodeModeLimits(), max_child_calls=1, max_concurrent_calls=1) if failure == "budget" else None
    result = NodeWorkerRuntime().execute("return await tools.browser_click(inputs);", catalog=catalog.entries,
                                        inputs=args,
                                        invoke=_browser_host(catalog.entries, invoke).invoke,
                                        limits=limits)
    assert not result.ok, result
    assert not calls


@pytest.mark.parametrize("raises", [False, True])
def test_read_failure_returns_failure_instead_of_successful_click(raises: bool) -> None:
    session = TaskSession(TaskPolicy(GRANTS), task_kind="chat")
    catalog = snapshot_catalog(offer_browser_tools(build_turn_snapshot(session), session))
    calls: list[str] = []

    def invoke(name: str, _args: JsonObject) -> ToolResult:
        calls.append(name)
        call = ToolCall(name, {})
        if name == "browser_read":
            if raises:
                raise ValueError("read unavailable")
            return ToolResult(ok=False, call=call, model_text="read unavailable")
        return ToolResult(ok=True, call=call, model_text="clicked", canonical={"value": _valid_browser_value(name)})

    result = NodeWorkerRuntime().execute("return await tools.browser_click(inputs);", catalog=catalog.entries,
                                        inputs={"ref": "observed", "purpose": "result"},
                                        invoke=_browser_host(catalog.entries, invoke).invoke)
    assert result.ok and calls == ["browser_click", "browser_read"]
    value = decode_json_object(result.value)
    assert value["ok"] is False and "do not repeat" in str(value["error"])


def test_unawaited_composite_is_still_rejected() -> None:
    session = TaskSession(TaskPolicy(GRANTS), task_kind="chat")
    catalog = snapshot_catalog(offer_browser_tools(build_turn_snapshot(session), session))

    def invoke(_name: str, _args: dict[str, object]) -> object:
        return {"ok": True, "value": "leaf"}

    result = NodeWorkerRuntime().execute("tools.browser_click(inputs); return null;", catalog=catalog.entries,
                                        inputs={"ref": "observed", "purpose": "result"}, invoke=invoke)
    assert not result.ok and result.error_code == "unawaited_calls", result


def test_missing_wait_arguments_offer_the_same_executable_purposes_on_both_surfaces() -> None:
    session = TaskSession(TaskPolicy(GRANTS, required_checks=(require_tool_success("browser_wait"),)), task_kind="chat")
    snapshot = offer_browser_tools(build_turn_snapshot(session), session)
    direct = next(spec for spec in snapshot.model_specs if spec.name == "browser_click")
    entry = next(entry for entry in snapshot_catalog(snapshot).entries if entry.name == "browser_click")
    direct_fields = canonical_json_object(dict(direct.parameters))
    nested_fields = canonical_json_object(entry.parameters["properties"])
    assert direct_fields["purpose"] == nested_fields["purpose"] == {"type": "string", "enum": ["controls", "action"]}


def test_composite_freezes_arguments_once_before_selecting_the_plan() -> None:
    session = TaskSession(TaskPolicy(GRANTS), task_kind="chat")
    catalog = snapshot_catalog(offer_browser_tools(build_turn_snapshot(session), session))
    calls: list[tuple[str, JsonObject]] = []

    def invoke(name: str, args: JsonObject) -> ToolResult:
        calls.append((name, decode_json_object(args)))
        return ToolResult(ok=True, call=ToolCall(name, decode_json_object(args)), model_text="leaf",
                          canonical={"value": _valid_browser_value(name)})

    result = NodeWorkerRuntime().execute(
        'let reads=0; const args={ref:"observed", get purpose(){reads++; return reads===1?"controls":"result";}}; '
        'await tools.browser_click(args); return reads;', catalog=catalog.entries,
        invoke=_browser_host(catalog.entries, invoke).invoke)
    assert result.ok and result.value == 1, result
    assert calls == [("browser_click", {"ref": "observed"}), ("browser_snapshot", {}), ("browser_read", {})]


@pytest.mark.parametrize(("purpose", "modal"), [(None, False), ("action", False), ("controls", False),
                                               ("result", False), ("result", True)])
def test_link_timeout_settles_one_click_then_observes_without_replay(purpose: str | None, modal: bool) -> None:
    session = TaskSession(TaskPolicy(GRANTS), task_kind="hybrid")
    catalog = snapshot_catalog(offer_browser_tools(build_turn_snapshot(session), session))
    document: JsonObject = {"target_id": "page", "frame_id": "main", "loader_id": "old", "url": "https://example.test"}
    calls: list[tuple[str, JsonObject]] = []
    scope: JsonObject = {"root_ref": "", "selector": ""}
    initial = ToolResult(ToolCall("browser_snapshot", {}), "", ok=True, audit={"browser_document": document},
                         canonical={"value": {"scope": scope, "elements_truncated": False, "contexts": []}})
    session._memory_results["initial"] = initial
    project_execution_record(session, "initial", initial.call, initial, ok=True)

    def invoke(name: str, args: JsonObject) -> ToolResult:
        calls.append((name, args))
        call = ToolCall(name, args)
        ref = name.removeprefix("browser_")
        audit: JsonObject = {"result_ref": ref, "browser_document": document}
        value = _valid_browser_value(name)
        if name == "browser_click":
            audit["link_origin"] = document
        elif name == "browser_wait":
            value = {"reason": "timeout"}
        elif name == "browser_snapshot":
            contexts: list[JsonValue] = [{"id": "modal", "kind": "dialog"}] if modal else []
            value = {**decode_json_object(value), "scope": scope, "contexts": contexts}
        result = ToolResult(call, name, ok=name != "browser_wait", audit=audit, canonical={"value": value})
        session._memory_results[ref] = result
        project_execution_record(session, ref, call, result, ok=result.ok)
        return result

    args: JsonObject = {"ref": "observed", **({"purpose": purpose} if purpose else {})}
    reply = NodeWorkerRuntime().execute("return await tools.browser_click(inputs);", catalog=catalog.entries,
        inputs=args, invoke=_browser_host(catalog.entries, invoke, link_result_observed=lambda click, observation:
            observed_link_outcome(session, click, document, through=observation)).invoke)
    assert reply.ok and decode_json_object(reply.value)["ok"] is (purpose != "result" or modal)
    assert calls == [("browser_click", {"ref": "observed"}),
        ("browser_wait", {"condition": "new_document", "timeout_ms": 3000, "value": "click"}), ("browser_snapshot", {}),
        *([("browser_read", {})] if modal else [])]
    assert pending_link_click(session, "[observed]", document) == ("" if modal else "click")
    assert not pending_link_click(session, "observed", {**document, "loader_id": "new"})


def test_same_document_modal_settles_the_original_link_click() -> None:
    session = TaskSession(TaskPolicy(GRANTS), task_kind="hybrid")
    document: JsonObject = {"target_id": "page", "frame_id": "main", "loader_id": "old", "url": "https://example.test"}
    scope: JsonObject = {"root_ref": "", "selector": ""}

    def receipt(ref: str, tool: str, args: JsonObject, value: JsonObject, *, ok: bool = True) -> None:
        call = ToolCall(tool, args)
        audit: JsonObject = {"browser_document": document}
        if tool == "browser_click":
            audit["link_origin"] = document
        result = ToolResult(call, "", ok=ok, audit=audit, canonical={"value": value})
        session._memory_results[ref] = result
        project_execution_record(session, ref, call, result, ok=ok)

    receipt("initial", "browser_snapshot", {}, {"scope": scope, "elements_truncated": False, "contexts": []})
    receipt("click", "browser_click", {"ref": "[observed]"}, {"delivery": "delivered"})
    receipt("wait", "browser_wait", {"condition": "new_document", "value": "click"}, {"reason": "timeout"}, ok=False)
    assert pending_link_click(session, "observed", document) == "click"
    receipt("unrelated", "browser_snapshot", {}, {"scope": scope, "elements_truncated": False,
                                                "contexts": [{"id": "area", "kind": "article"}]})
    assert pending_link_click(session, "observed", document) == "click"
    receipt("dialog", "browser_snapshot", {}, {"scope": scope, "elements_truncated": False,
                                             "contexts": [{"id": "modal", "kind": "dialog"}]})
    assert not pending_link_click(session, "observed", document)
    assert not observed_link_outcome(session, "click", document, through="unrelated")


def _valid_browser_value(name: str) -> JsonValue:
    if name == "browser_read":
        return "leaf"
    if name == "browser_snapshot":
        return {
            "page_id": "page", "page_url": "https://example.test", "generation": 1, "text": "leaf",
            "complete": True, "scope": "page", "text_truncated": False, "elements_truncated": False,
            "elements": [], "contexts": [], "file_controls": [],
        }
    if name == "browser_wait":
        return {"action": "wait", "condition": "text", "matched": True,
                "page_url": "https://example.test", "text": "leaf"}
    return {"action": "click", "delivery": "delivered", "text": "leaf", "ref": "observed",
            "observation": "leaf", "effect_verified": True, "page_url": "https://example.test"}


def _browser_host(catalog: Sequence[ToolCatalogEntry], invoke: Callable[[str, JsonObject], ToolResult], *,
                  link_result_observed: Callable[[str, str], bool] | None = None) -> CodeModeHost:
    def execute(call: ToolCall) -> ToolResult:
        return invoke(call.name, call.args)

    return CodeModeHost(execution_id="test", catalog=catalog, execute=execute, allow_writes=True,
                        link_result_observed=link_result_observed)
