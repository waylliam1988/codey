"""Receipt-derived next steps use the production provider, kernel and Chromium."""
from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
from collections.abc import Iterator, Sequence
from dataclasses import replace
from pathlib import Path
from typing import NoReturn
from unittest.mock import patch

import pytest
from playwright.sync_api import sync_playwright

from codey.automation.browser_contracts import RouteDecision as Route
from codey.automation.browser_session import BrowserToolOutput
from codey.codemode.kernel import build_execute_turn_executor
from codey.operations.browser.browser_followups import click_followup_plan
from codey.operations.browser.browser_task_handoff import browser_task_handoff, initial_navigation
from codey.operations.browser.browser_text_report import (
    BrowserMetadata,
    MetadataPending,
    observe_metadata,
    report_scope,
)
from codey.operations.browser.turn_surface import offer_browser_call, offer_browser_tools
from codey.operations.completion_context import CompletionContext
from codey.operations.completion_output import prepare_completion_output
from codey.operations.kernel.kernel_context import KernelContext
from codey.operations.kernel.kernel_execution import execute_turn
from codey.operations.kernel.kernel_protocol import normalize_turn
from codey.operations.kernel.turn_contract import TurnSnapshot
from codey.operations.kernel_contracts import KernelExecutionDeps, KernelRunRequest, KernelTransportDeps
from codey.operations.kernel_facts import project_execution_record
from codey.operations.provider_receipts import restore_snapshot, snapshot_description
from codey.operations.task_session import TaskSession
from codey.operations.turn_snapshot import build_turn_snapshot
from codey.policies.task_policy import TaskPolicy
from codey.policies.tool_requirement import parse_tool_success_check, require_tool_success
from codey.protocols.text_turn import decode_text_turn
from codey.research.tools import ResearchTools
from codey.runtime.core.models import ToolCall, ToolResult
from codey.runtime.observe.events import RunEvent
from codey.storage.managed_outputs import ManagedOutputStore
from codey.task.web_intent import explicit_web_intent
from codey.types.json_value import (
    JsonObject,
    decode_json_object,
    decode_json_value,
    parse_json_object,
    require_json_string,
)
from tests.support.browser_reconnect_process import FixtureBrowser, NoModel, new_session, resources
from tests.support.real_cdp import cdp_browser
from tests.test_receipt_bound_edit_surface_probe import install_http
from tools.browser_prerequisite_ab import LOAD, READ, WAIT, code_action
from tools.browser_production_live_gate import URL, run_case
from tools.completion_artifact_audit import tool_receipts
from tools.completion_explanation_ab import Target

GRANTS = frozenset({"control", "web.read", "browser.observe", "browser.interact", "browser.effect"})
TARGET = Target("http://scripted.test/v1", "scripted-fixture", 123456789, 15)


def remember(session: TaskSession, identity: str, call: ToolCall, value: JsonObject, document: JsonObject) -> None:
    receipt = ToolResult(call, "observed", ok=True, canonical={"value": value}, audit={"browser_document": document})
    session._memory_results[identity] = receipt
    project_execution_record(session, identity, call, receipt, ok=True)


@pytest.mark.parametrize("name,args", [
    ("browser_wait", {"condition": "text", "value": "Ready", "timeout_ms": 5000}),
    ("browser_tab_list", {}), ("browser_eval", {"code": "window.token"}), ("browser_download", {"ref": "observed"}),
])
def test_single_step_lowering_preserves_frozen_leaf_and_snapshot(name: str, args: JsonObject) -> None:
    session = TaskSession(TaskPolicy(GRANTS), task_kind="chat")
    base = build_turn_snapshot(session)
    offered = offer_browser_call(base, session, name)
    assert offered.nested_tool_names == base.nested_tool_names
    assert restore_snapshot(snapshot_description(offered)) == offered
    model_spec = next(spec for spec in offered.model_specs if spec.name == name)
    leaf_spec = next(spec for spec in base.frozen_specs if spec.name == name)
    assert replace(model_spec, description=leaf_spec.description) == leaf_spec
    assert model_spec.description.startswith(leaf_spec.description)
    action: JsonObject = {"tool": name, "args": args}
    plan = normalize_turn(decode_text_turn(json.dumps(action)), snapshot=offered)
    assert not plan.protocol_error
    assert len(plan.calls) == 1 and plan.calls[0].name == "codemode"
    assert plan.calls[0].args == {"code": f"return unwrap(await tools.{name}(inputs));", "inputs": args, "language": "javascript"}
    session.controller_denied = (name,)
    assert offer_browser_call(base, session, name) == base
    session.controller_denied = ()
    session.model_tool_names = ("codemode", "done")
    assert offer_browser_call(base, session, name) == base


@pytest.mark.parametrize("task,expected", [
    (f"Navigate to {URL}", True), (f"Navigate to {URL}. Use codemode only.", True),
    (f'Navigate to "{URL}"', True),
    (f"Do not navigate to {URL}", False), (f'Example: "Navigate to {URL}"', False),
    (f"> Navigate to {URL}", False), (f"`Navigate to {URL}`", False),
    (f"Navigate to {URL} and https://example.com/other", False),
])
def test_navigation_binding_uses_explicit_unambiguous_intent(task: str, expected: bool) -> None:
    session = TaskSession(TaskPolicy(GRANTS, required_checks=(require_tool_success("browser_navigate"),)),
                          task_kind="chat", task_text=task, required_source_urls=explicit_web_intent(task).urls)
    snapshot = build_turn_snapshot(session)
    call = initial_navigation(session, snapshot)
    assert (call is not None) is expected
    if expected and "only" in task:
        assert call is not None and call.name == "codemode"
        offered = offer_browser_tools(snapshot, session)
        assert offered.tool_names == snapshot.tool_names and offered.browser.followups
        assert restore_snapshot(snapshot_description(offered)) == offered


@pytest.mark.parametrize("kind", ["bare", "bound", "denied"])
def test_wait_plan_exposes_missing_arguments_and_unavailable_leaves(kind: str) -> None:
    checks = (require_tool_success("browser_wait", arguments={} if kind == "bare" else {
        "condition": "text", "value": "Ready"}),)
    names = {"browser_snapshot", "browser_read"} | ({"browser_wait"} if kind != "denied" else set())
    plan = click_followup_plan(checks, names)
    assert ("result" in plan.plans) is (kind == "bound")
    assert bool(plan.missing_waits) is (kind == "bare")
    assert plan.unavailable == (("browser_wait",) if kind == "denied" else ())
    if kind == "bound":
        assert plan.plans["result"] == [
            {"name": "browser_wait", "args": {"condition": "text", "value": "Ready", "timeout_ms": 5000}},
            {"name": "browser_read", "args": {}},
        ]


@pytest.mark.parametrize("tool,pointer,value,args", [
    ("browser_tab_list", "/page_url", {"page_url": URL, "tabs": [], "text": "tabs"}, {}),
    ("browser_eval", "/value", {"value": "TOKEN", "truncated": False}, {"code": "window.token"}),
])
def test_scalar_metadata_binding_requires_the_current_document(
    tool: str, pointer: str, value: JsonObject, args: JsonObject,
) -> None:
    rule = parse_tool_success_check(require_tool_success(tool, arguments=args,
        answer_pointer="/field", result_pointer=pointer))
    assert rule is not None
    session = TaskSession(TaskPolicy(GRANTS))
    document: JsonObject = {"target_id": "tab", "frame_id": "main", "loader_id": "first", "url": URL}
    remember(session, "metadata", ToolCall(tool, args, "metadata"), value, document)
    assert rule.observe(session.executed, results=session._memory_results, browser_document=document).evaluation.passed
    documents: tuple[JsonObject | None, ...] = (None, {"target_id": "tab", "frame_id": "main", "loader_id": "reloaded", "url": URL})
    for current in documents:
        observation = rule.observe(session.executed, results=session._memory_results, browser_document=current)
        assert observation.evaluation.reason_code == "observation_selector_unavailable"


def test_metadata_conflict_requires_recovery_instead_of_another_execution() -> None:
    session = TaskSession(TaskPolicy(GRANTS))
    rule = parse_tool_success_check(require_tool_success("browser_eval", arguments={"code": "window.token"},
        answer_pointer="/token", result_pointer="/value"))
    assert rule is not None
    document: JsonObject = {"target_id": "tab", "frame_id": "main", "loader_id": "current", "url": URL}
    for identity in ("first", "different"):
        remember(session, identity, ToolCall("browser_eval", {"code": "window.token"}, identity),
                 {"value": identity, "truncated": False}, document)
    assert rule.observe(session.executed, results=session._memory_results,
                        browser_document=document).evaluation.reason_code == "conflicting_observations"


@pytest.mark.parametrize("state", ["fresh", "generation", "document", "truncated", "multiple"])
def test_download_candidates_require_current_complete_snapshot(state: str) -> None:
    session = TaskSession(TaskPolicy(GRANTS, required_checks=(require_tool_success("browser_download"),)), task_kind="chat")
    document: JsonObject = {"target_id": "tab", "frame_id": "main", "loader_id": "current", "url": URL}
    value: JsonObject = {"generation": 2, "complete": state != "truncated", "elements_truncated": False,
        "elements": [{"name": "First", "ref": "real-first", "role": "link", "disabled": False}]}
    if state == "multiple":
        elements = value["elements"]
        assert isinstance(elements, list)
        elements.append({"name": "Second", "ref": "real-second", "role": "button", "disabled": False})
    remember(session, "snapshot", ToolCall("browser_snapshot", {}, "snapshot"), value,
             {"target_id": "tab", "frame_id": "main", "loader_id": "old", "url": URL} if state == "document" else document)
    _, hint = browser_task_handoff(session, build_turn_snapshot(session), CompletionContext(browser_document=document),
                                  3 if state == "generation" else 2)
    if state in {"generation", "document", "truncated"}:
        assert '"tool": "browser_snapshot", "args": {}' in hint
        assert "real-first" not in hint
    else:
        assert "real-first" in hint and "browser_download" in hint
        if state == "multiple":
            assert "real-second" in hint and "supply ref" in hint


@pytest.mark.usefixtures("public_source_dns", "scripted_local_api_connection", "no_external_advisor_models")
@pytest.mark.parametrize("json_output", [False, True])
def test_current_body_hands_off_to_real_metadata_calls_without_repeating_work(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, json_output: bool,
) -> None:
    snapshots: list[TurnSnapshot] = []
    handoffs: list[str] = []
    original = KernelContext.request_snapshot

    def capture(context: KernelContext, snapshot: TurnSnapshot, *,
                delivered_results: Sequence[ToolResult] = ()) -> tuple[TurnSnapshot, str]:
        result = original(context, snapshot, delivered_results=delivered_results)
        snapshots.append(result[0])
        handoffs.append(result[1])
        return result

    monkeypatch.setattr(KernelContext, "request_snapshot", capture)

    def replies() -> Iterator[JsonObject]:
        yield code_action(LOAD + WAIT + f"return {READ};")
        for _ in range(2):
            snapshot = snapshots[-1]
            assert restore_snapshot(snapshot_description(snapshot)) == snapshot
            assert "browser_read" not in snapshot.tool_names and "browser_click" not in snapshot.tool_names
            assert "browser_read" in snapshot.nested_tool_names
            offered, = set(snapshot.tool_names) & {"browser_eval", "browser_tab_list"}
            if offered == "browser_eval" and not json_output:
                yield code_action("return unwrap(await tools.browser_eval({code:'window.other'}));")
                assert "All declared tool executions have receipts" not in handoffs[-1]
                assert "browser_eval" in snapshots[-1].tool_names
            yield {"tool": offered, "args": {"code": "window.__contractToken"} if offered == "browser_eval" else {}}
        yield {"tool": "done", "args": ({"paragraphs": ["已读取当前记录。", "净重 173 克，校验码 QUARTZ_851。"]}
                                         if json_output else {"summary": "已读取当前记录。\n\n净重 173 克，校验码 QUARTZ_851。"})}

    install_http(monkeypatch, replies())
    directory = tmp_path / "metadata"
    row = run_case(directory, "chat", TARGET, structured=not json_output, explanation_output=json_output, max_turns=5)
    assert row["passed"], row
    receipts = tool_receipts(directory / "state")
    children = [receipt for receipt in receipts if receipt.result.call.name in {"browser_eval", "browser_tab_list"}]
    assert len(children) == (2 if json_output else 3) and all(receipt.result.ok for receipt in children)
    for child in children:
        assert any(parent.result.ok and isinstance(refs := parent.result.canonical.get("child_effect_ids"), list)
                   and child.identity in refs for parent in receipts if parent.result.call.name == "codemode")
    for name in ("browser_click", "browser_read"):
        assert len([receipt for receipt in receipts if receipt.result.call.name == name]) == 1


def test_text_metadata_pending_uses_the_same_reason_and_exact_expression() -> None:
    session = TaskSession(TaskPolicy(GRANTS), task_kind="chat", task_text=(
        "用 browser_eval 读取 window.token，用 browser_tab_list 获取当前标签页 URL；报告 token 和 URL。"))
    scope = report_scope(session)
    assert scope is not None
    document: JsonObject = {"target_id": "tab", "frame_id": "main", "url": URL, "loader_id": "current"}
    pending = observe_metadata(scope, session.executed, session._memory_results, document)
    assert isinstance(pending, MetadataPending) and pending.call == ToolCall("browser_tab_list", {})
    remember(session, "tabs", ToolCall("browser_tab_list", {}, "tabs"), {"page_url": URL}, document)
    pending = observe_metadata(scope, session.executed, session._memory_results, document)
    assert isinstance(pending, MetadataPending) and pending.call == ToolCall("browser_eval", {"code": "window.token"})
    remember(session, "wrong", ToolCall("browser_eval", {"code": "window.other"}, "wrong"),
             {"value": "WRONG", "truncated": False}, document)
    assert isinstance(observe_metadata(scope, session.executed, session._memory_results, document), MetadataPending)
    remember(session, "token", ToolCall("browser_eval", {"code": "window.token"}, "token"),
             {"value": "TOKEN", "truncated": False}, document)
    assert observe_metadata(scope, session.executed, session._memory_results, document) == BrowserMetadata(URL, "TOKEN")
    output = prepare_completion_output(session, "正文。", CompletionContext(browser_document=document))
    assert "TOKEN" in output.text


@pytest.mark.usefixtures("public_source_dns", "scripted_local_api_connection", "no_external_advisor_models")
@pytest.mark.parametrize("only", [False, True])
def test_download_uses_observed_ref_and_retains_real_artifact(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, only: bool,
) -> None:
    directory = tmp_path / "download"

    def replies() -> Iterator[JsonObject]:
        yield {"tool": "codemode", "args": {"code": "return unwrap(await tools.browser_snapshot({}));"}}
        snapshots = [row.result for row in tool_receipts(directory / "state") if row.result.call.name == "browser_snapshot"]
        elements = decode_json_object(snapshots[-1].canonical["value"])["elements"]
        assert isinstance(elements, list)
        ref = next(element["ref"] for element in elements if isinstance(element, dict) and element.get("name") == "Download fixture")
        args: JsonObject = {"ref": ref}
        yield ({"tool": "codemode", "args": {"code": "return unwrap(await tools.browser_download(inputs));", "inputs": args}}
               if only else {"tool": "browser_download", "args": args})
        yield {"tool": "codemode", "args": {"code": "return unwrap(await tools.browser_read({}));"}}
        download, = [row.result for row in tool_receipts(directory / "state") if row.result.call.name == "browser_download"]
        yield {"tool": "done", "args": {"summary": json.dumps(download.canonical["value"], ensure_ascii=False)}}

    def task_text(task: str) -> str:
        return task if only else task.replace("Use codemode only:", "Use the browser tools:")

    install_http(monkeypatch, replies())
    row = run_case(directory, "chat", TARGET, download_file=True, task_transform=task_text, max_turns=4)
    assert row["passed"], row
    receipts = tool_receipts(directory / "state")
    download, = [row.result for row in receipts if row.result.call.name == "browser_download"]
    value = decode_json_object(download.canonical["value"])
    content = Path(require_json_string(value["path"], "path")).read_bytes()
    assert value["bytes"] == len(content) and value["sha256"] == hashlib.sha256(content).hexdigest()


@pytest.mark.usefixtures("public_source_dns")
def test_download_storage_failure_preserves_delivery_and_does_not_offer_a_retry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider = FixtureBrowser(headless=True)
    browser = provider.browser
    tools = ResearchTools(search=provider, store=None, changes=None, browser_session=browser)
    session = new_session()
    session.policy = replace(session.policy, required_checks=(require_tool_success("browser_download"),))
    sink = resources(tmp_path / "state", initialize=True)
    calls = 0
    original = browser.download

    def route_document(route: Route) -> None:
        route.fulfill(content_type="text/html", body=(
            "<a download='fixture.bin' href='data:application/octet-stream;base64,cmVhbCBieXRlcw=='>Download</a>"))

    def download(ref: str) -> BrowserToolOutput:
        nonlocal calls
        calls += 1
        return original(ref)

    def fail_storage(store: ManagedOutputStore, *, session_id: str, run_id: str, raw: bytes,
                     permission_profile: str) -> NoReturn:
        raise OSError("fixture storage failure")

    monkeypatch.setattr(provider, "_guard_request", route_document)
    monkeypatch.setattr(browser, "download", download)
    monkeypatch.setattr(ManagedOutputStore, "write_binary_artifact", fail_storage)
    snapshot = build_turn_snapshot(session)
    parent = build_execute_turn_executor(session=session, snapshot=snapshot, run_id="r", effect_scope="task", turn=1,
        research_tools=tools, intent_sink=sink, durable=sink.durable, managed_outputs=sink.managed_outputs, session_id="s")
    call = ToolCall("codemode", {"code": "unwrap(await tools.browser_navigate({url:inputs.url}));"
        "const s=unwrap(await tools.browser_snapshot({}));"
        "return unwrap(await tools.browser_download({ref:s.elements[0].ref}));", "inputs": {"url": URL}}, "download")
    try:
        result = execute_turn(session, [call], snapshot=snapshot, run_id="r", session_id="s", effect_scope="task", turn=1,
            research_tools=tools, executors={"codemode": parent}, intent_sink=sink,
            managed_outputs=sink.managed_outputs, durable=sink.durable)[0]
        assert not result.ok and calls == 1
        _, hint = browser_task_handoff(session, snapshot, CompletionContext(browser_document=browser.document_identity()),
                                      browser.generation)
        assert '"delivery": "delivered"' in hint and '"storage": "failed"' in hint
        assert "Outstanding declared calls: browser_download" not in hint
        assert all(artifact.metadata.get("kind") == "browser_progress" for artifact in sink.durable.artifacts_for_task("r"))
    finally:
        sink.durable.close()
        provider.close()


@pytest.mark.usefixtures("public_source_dns", "scripted_local_api_connection", "no_external_advisor_models")
@pytest.mark.parametrize("nested,reread", [(False, False), (True, False), (False, True)])
def test_declared_wait_composes_click_wait_read_on_an_async_page(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, nested: bool, reread: bool,
) -> None:
    directory = tmp_path / "bound"
    html = """<meta charset="utf-8"><label>Locality<input></label><article>Old record 101 grams OLD_101</article>
<button onclick="this.dataset.count=Number(this.dataset.count||0)+1;
setTimeout(()=>document.querySelector('article').textContent='福州鱼丸净重 173 克 QUARTZ_851',450)">Load record</button>"""

    def replies() -> Iterator[JsonObject]:
        prefix = f"{READ};" if reread else ""
        yield code_action(prefix + LOAD[:LOAD.index("unwrap(await tools.browser_click")] + "return s;")
        snapshots = [receipt.result for receipt in tool_receipts(directory / "state")
                     if receipt.result.call.name == "browser_snapshot"]
        elements = decode_json_object(snapshots[-1].canonical["value"])["elements"]
        assert isinstance(elements, list)
        ref = next(item["ref"] for item in elements if isinstance(item, dict) and item.get("name") == "Load record")
        args: JsonObject = {"ref": ref, "purpose": "result"}
        yield ({"tool": "codemode", "args": {"code": "return unwrap(await tools.browser_click(inputs));", "inputs": args}}
               if nested else {"tool": "browser_click", "args": args})
        yield {"tool": "done", "args": {"summary": "净重 173 克，校验码 QUARTZ_851。"}}

    install_http(monkeypatch, replies())
    row = run_case(directory, "chat", TARGET, max_turns=3, reread=reread, fixture_html=html,
        additional_required_tools=(require_tool_success("browser_wait", arguments={"condition": "text", "value": "鱼丸"}),))
    assert row["passed"], row
    receipts = tool_receipts(directory / "state")
    sequence = [receipt.result for receipt in receipts
                if receipt.result.call.name in {"browser_click", "browser_wait", "browser_read"}]
    assert [receipt.call.name for receipt in sequence] == (["browser_read"] if reread else []) + [
        "browser_click", "browser_wait", "browser_read"]
    assert all(receipt.ok for receipt in sequence)
    assert "173" in sequence[-1].model_text and "OLD_101" not in sequence[-1].model_text


def interrupted_action_probe(state: Path, port: int, operation: str, stage: str) -> None:
    initial = stage != "resume"
    sink = resources(state, initialize=initial)
    session = new_session()
    query = operation == "query"
    tool = "browser_click" if query else operation
    session.policy = replace(session.policy, required_checks=(require_tool_success("browser_wait", arguments={
        "condition": "text", "value": "Ready"} if query else {}),)
        if tool == "browser_click" else (require_tool_success("browser_download"),))

    class ActionBrowser(FixtureBrowser):
        def _guard_request(self, route: Route) -> None:
            route.fulfill(content_type="text/html", body=(
                "<article>Old body</article><a download='fixture.bin' href='data:application/octet-stream;base64,cmVhbCBieXRlcw==' "
                "onclick=\"this.dataset.count=Number(this.dataset.count||0)+1;"
                "setTimeout(()=>document.querySelector('article').textContent='Ready: current record is 29 grams.',200)\">Download</a>"))

    provider = ActionBrowser(headless=False, isolated=False, launch=False, require_existing_session=True, cdp_port=port)
    browser = provider.browser
    tools = ResearchTools(search=provider, store=None, changes=None, browser_session=browser)
    context = KernelContext(session, KernelRunRequest(
        transport=KernelTransportDeps(provider=NoModel(), run_id="r", effect_scope="task", provider_id="local"),
        execution=KernelExecutionDeps(intent_sink=sink, research_tools=tools)))
    context.restore_facts()
    if initial:
        base = build_turn_snapshot(session)
        snapshot_result = execute_turn(session, [ToolCall("browser_navigate", {"url": URL}), ToolCall("browser_snapshot", {})],
            snapshot=replace(base, tool_names=(*base.tool_names, "browser_navigate", "browser_snapshot")),
            run_id="r", session_id="s", effect_scope="fixture", turn=1, research_tools=tools)[-1]
        assert snapshot_result.ok
        elements = decode_json_object(snapshot_result.canonical["value"])["elements"]
        assert isinstance(elements, list)
        ref = next(row["ref"] for row in elements if isinstance(row, dict) and row.get("role") == "link")
        base = build_turn_snapshot(session)
        snapshot = offer_browser_tools(base, session) if query else offer_browser_call(base, session, tool)
        args: JsonObject = {"ref": ref}
        if query:
            args["purpose"] = "result"
        call = ToolCall("codemode", {"code": f"return unwrap(await tools.{tool}(inputs));", "inputs": args}, "action")
        saved_request: dict[str, object] = {"snapshot": snapshot_description(snapshot), "args": call.args}
        (state / "request.json").write_text(json.dumps(saved_request), encoding="utf-8")
    else:
        saved = parse_json_object((state / "request.json").read_text(encoding="utf-8"))
        snapshot = restore_snapshot(saved["snapshot"])
        call = ToolCall("codemode", decode_json_object(saved["args"]), "action")

    def after_receipt(event: RunEvent) -> None:
        if initial and stage == "after" and event.kind == "tool" and event.call is not None and event.call.name == tool:
            os._exit(73)

    parent = build_execute_turn_executor(session=session, snapshot=snapshot, run_id="r", effect_scope="task", turn=2,
        research_tools=tools, intent_sink=sink, durable=sink.durable, managed_outputs=sink.managed_outputs,
        session_id="s", on_event=after_receipt)

    def dispatch() -> ToolResult:
        return execute_turn(session, [call], snapshot=snapshot, run_id="r", session_id="s", effect_scope="task", turn=2,
            research_tools=tools, executors={"codemode": parent}, intent_sink=sink,
            managed_outputs=sink.managed_outputs, durable=sink.durable)[0]

    if initial and stage == "before":
        original = browser.download

        def delivered(ref: str) -> BrowserToolOutput:
            original(ref)
            os._exit(74)

        with patch.object(browser, "download", delivered):
            dispatch()
        raise AssertionError("expected interrupted process")
    result = dispatch()
    assert not initial
    outcome: JsonObject = {"ok": result.ok, "text": result.model_text,
        "value": decode_json_value(result.canonical.get("value"))}
    if result.ok:
        count = browser.evaluate("document.querySelector('a').dataset.count")
        assert count.ok, count.model_text
        outcome["effect_count"] = count.observation["value"]
    else:
        # Unknown dispatch is inspected independently before the durable owner
        # uses its saved progress binding to clean up its page.
        with sync_playwright() as observer:
            observed = observer.chromium.connect_over_cdp(f"http://127.0.0.1:{port}")
            page = next(item for item in observed.contexts[0].pages if item.url == URL)
            outcome["effect_count"] = page.locator("a").get_attribute("data-count")
    if operation == "browser_click":
        _, hint = browser_task_handoff(session, build_turn_snapshot(session),
            CompletionContext(browser_document=browser.document_identity()), browser.generation)
        outcome["handoff"] = hint
        assert "was delivered" in hint and "browser_wait next" in hint, hint
    if query:
        children = [row.call.name for row in session._memory_results.values()
                    if row.call.name in {"browser_click", "browser_wait", "browser_read"}]
        assert children == ["browser_click", "browser_wait", "browser_read"], children
    (state / "result.json").write_text(json.dumps(outcome), encoding="utf-8")
    sink.durable.close()


@pytest.mark.parametrize("operation,stage", [("browser_click", "after"), ("query", "after"),
                                             ("browser_download", "after"), ("browser_download", "before")])
def test_process_recovery_preserves_delivered_action_without_clicking_again(tmp_path: Path, operation: str, stage: str) -> None:
    state = tmp_path / "state"
    with cdp_browser(tmp_path / "profile") as port:
        source = ("from pathlib import Path; import sys; "
                  "from tests.test_browser_handoff_next_steps import interrupted_action_probe; "
                  "interrupted_action_probe(Path(sys.argv[1]),int(sys.argv[2]),sys.argv[3],sys.argv[4])")
        command = [sys.executable, "-c", source, str(state), str(port), operation]
        interrupted = subprocess.run([*command, stage], capture_output=True, text=True, timeout=60)
        assert interrupted.returncode == (73 if stage == "after" else 74), interrupted.stdout + interrupted.stderr
        resumed = subprocess.run([*command, "resume"], capture_output=True, text=True, timeout=60)
        assert resumed.returncode == 0, resumed.stdout + resumed.stderr
        result = parse_json_object((state / "result.json").read_text(encoding="utf-8"))
        assert result["ok"] is (stage == "after"), result
        if stage == "after":
            assert result["effect_count"] == "1"
        if operation == "browser_download" and stage == "after":
            value = decode_json_object(result["value"])
            assert Path(require_json_string(value["path"], "path")).read_bytes() == b"real bytes"
            assert value["sha256"] == hashlib.sha256(b"real bytes").hexdigest()
        with sync_playwright() as observer:
            browser = observer.chromium.connect_over_cdp(f"http://127.0.0.1:{port}")
            pages = [page for page in browser.contexts[0].pages if page.url == URL]
            assert len(pages) <= 1
            if stage == "before":
                assert result["effect_count"] == "1"
            # The durable owner may dispose its owned page during cleanup.
            # Observe the action count in the resumed child before that cleanup.
            if pages:
                assert pages[0].locator("a").get_attribute("data-count") == "1"
