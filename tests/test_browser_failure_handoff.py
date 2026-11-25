"""Original failed programs recover through the existing production tool surface."""
from __future__ import annotations

import hashlib
import json
from collections.abc import Iterator, Sequence
from dataclasses import replace
from pathlib import Path

import pytest

from codey.operations.browser.browser_task_handoff import browser_task_handoff
from codey.operations.browser.turn_surface import offer_browser_tools
from codey.operations.completion_context import CompletionContext
from codey.operations.kernel.kernel_context import KernelContext
from codey.operations.kernel.kernel_protocol import TurnSnapshot
from codey.operations.kernel_facts import project_execution_record
from codey.operations.task_session import TaskSession
from codey.operations.turn_snapshot import build_turn_snapshot
from codey.policies.task_policy import TaskPolicy
from codey.policies.tool_requirement import require_tool_success
from codey.runtime.core.models import ToolCall, ToolResult
from codey.types.json_value import JsonObject, decode_json_object, parse_json_object, require_json_string
from tests.test_task_contract_fixed_replies import TARGET, install_original_http
from tools.browser_prerequisite_ab import LOAD, READ, WAIT, code_action
from tools.browser_production_live_gate import URL, run_case
from tools.completion_artifact_audit import tool_receipts

FIXTURE = Path(__file__).parent / "fixtures" / "browser_handoff_failure_replies_20261114.json"
GRANTS = frozenset({"control", "project.read", "project.write", "web.read", "browser.observe",
                    "browser.interact", "browser.effect"})
DOCUMENT: JsonObject = {"target_id": "task-tab", "loader_id": "task-document", "url": URL}


def original_reply(case: str) -> str:
    entry = decode_json_object(parse_json_object(FIXTURE.read_text(encoding="utf-8"))[case])
    return require_json_string(entry["reply"], "saved reply")


def next_step(handoff: str) -> JsonObject:
    actions = [parse_json_object(line.removeprefix("Execute this next step: ")) for line in handoff.splitlines()
               if line.startswith("Execute this next step: ")]
    assert len(actions) == 1, handoff
    return actions[0]


def remember(session: TaskSession, identity: str, result: ToolResult) -> None:
    session._memory_results[identity] = result
    project_execution_record(session, identity, result.call, result, ok=result.ok)


def compile_failure() -> ToolResult:
    return ToolResult(ToolCall("codemode", {"code": "invalid"}, "failed-program"), "Compilation failed", ok=False,
                      audit={"error_code": "syntax_error"}, canonical={"child_effect_ids": []})


def bound_session(*, kind: str = "chat", only: bool = False) -> TaskSession:
    session = TaskSession(TaskPolicy(GRANTS, required_checks=(require_tool_success("browser_snapshot"),)),
        task_kind=kind, task_text=f"Open <{URL}>." + (" Use codemode only." if only else ""),
        required_source_urls=frozenset({URL}))
    remember(session, "navigation", ToolResult(ToolCall("browser_navigate", {"url": URL}, "navigation"),
        "Opened", ok=True, canonical={"value": {"url": URL}}, audit={"browser_document": DOCUMENT}))
    return session


@pytest.fixture
def handoffs(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    captured: list[str] = []
    original = KernelContext.request_snapshot

    def capture(context: KernelContext, snapshot: TurnSnapshot, *,
                delivered_results: Sequence[ToolResult] = ()) -> tuple[TurnSnapshot, str]:
        result = original(context, snapshot, delivered_results=delivered_results)
        captured.append(result[1])
        return result

    monkeypatch.setattr(KernelContext, "request_snapshot", capture)
    return captured


@pytest.mark.parametrize("kind,only", [("chat", False), ("chat", True), ("hybrid", False)])
def test_compile_failure_offers_the_authorized_observation(kind: str, only: bool) -> None:
    session = bound_session(kind=kind, only=only)
    base = offer_browser_tools(build_turn_snapshot(session), session)
    _, hint = browser_task_handoff(session, base, CompletionContext(browser_document=DOCUMENT), 1,
                                  delivered_results=[compile_failure()])
    action = next_step(hint)
    assert action == ({"tool": "browser_snapshot", "args": {}} if not only else
        {"tool": "codemode", "args": {"code": "return unwrap(await tools.browser_snapshot(inputs));", "inputs": {}}})


@pytest.mark.parametrize("state", ["no_batch", "success", "other_error", "children", "missing_children", "unbound",
                                  "no_document", "other_document", "other_navigation", "failed_navigation",
                                  "click", "uncertain_upload", "download", "edit", "unconfirmed_edit", "research", "denied"])
def test_compile_recovery_requires_a_bound_page_and_preserves_actions(state: str) -> None:
    session = bound_session()
    failure = compile_failure()
    delivered: list[ToolResult] = [failure]
    document: JsonObject | None = DOCUMENT
    if state == "no_batch":
        delivered = []
    elif state == "success":
        delivered = [replace(failure, ok=True)]
    elif state == "other_error":
        delivered = [replace(failure, audit={"error_code": "result_contract"})]
    elif state == "children":
        delivered = [replace(failure, canonical={"child_effect_ids": ["child"]})]
    elif state == "missing_children":
        delivered = [replace(failure, canonical={})]
    elif state == "unbound":
        session.task_text = "Observe the current page"
        session.required_source_urls = frozenset()
    elif state == "no_document":
        document = None
    elif state == "other_document":
        document = {**DOCUMENT, "loader_id": "other"}
    elif state in {"other_navigation", "failed_navigation"}:
        remember(session, "later-navigation", ToolResult(ToolCall("browser_navigate",
            {"url": URL + "/other" if state == "other_navigation" else URL}, "later-navigation"), "Other",
            ok=state == "other_navigation", audit={"browser_document": DOCUMENT}))
    elif state in {"click", "uncertain_upload", "download", "edit", "unconfirmed_edit"}:
        name = ("browser_upload" if state == "uncertain_upload" else "edit" if state in {"edit", "unconfirmed_edit"}
                else "browser_" + state)
        canonical: JsonObject = ({"workspace_unconfirmed": True, "identity_reason": "scan failed"}
            if state == "unconfirmed_edit" else {"value": {
                "delivery": "uncertain" if state == "uncertain_upload" else "delivered"}})
        remember(session, "action", ToolResult(ToolCall(name, {}, "action"), "Sent",
            ok=state not in {"uncertain_upload", "unconfirmed_edit"}, canonical=canonical,
            audit={"browser_document": DOCUMENT}))
    elif state == "research":
        session.task_kind = "research"
        session.policy = replace(session.policy, strict_research=True)
    elif state == "denied":
        session.controller_denied = ("browser_snapshot",)
    remember(session, "failure", failure)
    _, hint = browser_task_handoff(session, build_turn_snapshot(session), CompletionContext(browser_document=document),
                                  1, delivered_results=delivered)
    assert "Execute this next step:" not in hint


@pytest.mark.parametrize("state", ["current", "generation", "document", "truncated"])
def test_compile_recovery_reuses_only_a_current_complete_snapshot(state: str) -> None:
    session = bound_session()
    value: JsonObject = {"generation": 2, "complete": state != "truncated", "elements_truncated": False,
                        "elements": [{"name": "Locality", "ref": "actual-locality", "role": "textbox"}]}
    remember(session, "snapshot", ToolResult(ToolCall("browser_snapshot", {}, "snapshot"), "Observed", ok=True,
        canonical={"value": value}, audit={"browser_document": {**DOCUMENT, "loader_id": "old"}
                                          if state == "document" else DOCUMENT}))
    _, hint = browser_task_handoff(session, build_turn_snapshot(session), CompletionContext(browser_document=DOCUMENT),
                                  3 if state == "generation" else 2, delivered_results=[compile_failure()])
    if state == "current":
        assert "Reuse the current snapshot from committed receipt snapshot" in hint
        assert "actual-locality" in hint and "Execute this next step:" not in hint
    else:
        assert "actual-locality" not in hint
        assert "browser_snapshot" in json.dumps(next_step(hint))


@pytest.mark.usefixtures("public_source_dns", "scripted_local_api_connection", "no_external_advisor_models")
@pytest.mark.parametrize("only", [False, True])
def test_original_basic_compile_failure_executes_the_offered_snapshot(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, handoffs: list[str], only: bool,
) -> None:
    directory = tmp_path / "basic"

    def replies() -> Iterator[str]:
        yield original_reply("basic")
        yield json.dumps(next_step(handoffs[-1]))
        snapshots = [row.result for row in tool_receipts(directory / "state")
                     if row.result.call.name == "browser_snapshot"]
        observed, = snapshots
        elements = decode_json_object(observed.canonical["value"])["elements"]
        assert isinstance(elements, list) and any(isinstance(item, dict) and item.get("name") == "Locality"
                                                for item in elements)
        yield json.dumps(code_action(LOAD + WAIT + f"return {READ};"))
        yield json.dumps({"tool": "done", "args": {"summary": "净重 173 克，校验码 QUARTZ_851。"}})

    install_original_http(monkeypatch, replies())
    row = run_case(directory, "chat", TARGET, max_turns=4,
                   task_transform=(lambda task: task + " Use codemode only.") if only else None)
    assert row["passed"], row
    receipts = tool_receipts(directory / "state")
    failure, = [r.result for r in receipts if r.result.audit.get("error_code") == "syntax_error"]
    assert failure.call.args == decode_json_object(parse_json_object(original_reply("basic"))["args"])
    assert failure.canonical["child_effect_ids"] == []
    assert len([r for r in receipts if r.result.call.name == "browser_click"]) == 1
    assert len([r for r in receipts if r.result.call.name == "browser_navigate"]) == 1


@pytest.mark.usefixtures("public_source_dns", "scripted_local_api_connection", "no_external_advisor_models")
def test_original_download_unpack_failure_reuses_the_snapshot_and_downloads_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, handoffs: list[str],
) -> None:
    directory = tmp_path / "download"

    def replies() -> Iterator[str]:
        yield original_reply("download")
        receipts = tool_receipts(directory / "state")
        observed, = [r.result for r in receipts if r.result.call.name == "browser_snapshot"]
        elements = decode_json_object(observed.canonical["value"])["elements"]
        assert isinstance(elements, list)
        ref = next(require_json_string(item["ref"], "ref") for item in elements
                   if isinstance(item, dict) and item.get("name") == "Download fixture")
        assert ref in handoffs[-1] and "Choose the requested download target" in handoffs[-1]
        yield json.dumps({"tool": "codemode", "args": {
            "code": "return unwrap(await tools.browser_download(inputs));", "inputs": {"ref": ref}}})
        yield json.dumps(code_action(f"return {READ};"))
        download, = [r.result for r in tool_receipts(directory / "state") if r.result.call.name == "browser_download"]
        yield json.dumps({"tool": "done", "args": {"summary": json.dumps(download.canonical["value"])}})

    install_original_http(monkeypatch, replies())
    row = run_case(directory, "chat", TARGET, download_file=True, max_turns=4)
    assert row["passed"], row
    receipts = tool_receipts(directory / "state")
    failed, = [r.result for r in receipts if r.result.call.name == "codemode" and not r.result.ok]
    original_args = decode_json_object(parse_json_object(original_reply("download"))["args"])
    assert failed.call.args == {**original_args, "language": "javascript"}
    assert failed.audit["error_code"] == "result_contract"
    assert len([r for r in receipts if r.result.call.name == "browser_snapshot"]) == 1
    download, = [r.result for r in receipts if r.result.call.name == "browser_download"]
    value = decode_json_object(download.canonical["value"])
    assert hashlib.sha256(Path(require_json_string(value["path"], "path")).read_bytes()).hexdigest() == value["sha256"]


@pytest.mark.usefixtures("public_source_dns", "scripted_local_api_connection", "no_external_advisor_models")
def test_compile_failure_after_a_delivered_click_only_completes_its_wait_and_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, handoffs: list[str],
) -> None:
    directory = tmp_path / "click"

    def replies() -> Iterator[str]:
        yield json.dumps(code_action(LOAD + "return null;"))
        yield original_reply("basic")
        wait = next_step(handoffs[-1])
        assert wait["tool"] == "browser_wait"
        yield json.dumps(wait)
        read = next_step(handoffs[-1])
        assert read["tool"] == "browser_read"
        yield json.dumps(read)
        yield json.dumps({"tool": "done", "args": {"summary": "净重 173 克，校验码 QUARTZ_851。"}})

    install_original_http(monkeypatch, replies())
    row = run_case(directory, "chat", TARGET, max_turns=5, additional_required_tools=(require_tool_success(
        "browser_wait", arguments={"condition": "text", "value": "鱼丸", "timeout_ms": 5000}),))
    assert row["passed"], row
    receipts = tool_receipts(directory / "state")
    assert len([r for r in receipts if r.result.call.name == "browser_click"]) == 1
    assert len([r for r in receipts if r.result.call.name == "browser_wait"]) == 1
