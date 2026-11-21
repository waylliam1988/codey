"""Explicit browser navigation is an initial runtime step without a tool checklist."""
from __future__ import annotations

import hashlib
from dataclasses import replace
from pathlib import Path

import pytest

from codey.operations.browser_task_handoff import initial_navigation
from codey.operations.kernel_protocol import build_turn_snapshot
from codey.operations.provider_receipts import restore_snapshot, snapshot_description
from codey.operations.task_session import TaskSession
from codey.policies.task_policy import TaskPolicy
from codey.policies.tool_requirement import parse_tool_success_check, require_tool_success
from codey.runtime.core.models import ToolCall, ToolResult
from codey.task.web_intent import explicit_web_intent
from codey.types.json_value import decode_json_object, parse_json_object, require_json_string, require_json_strings
from tests.test_task_contract_fixed_replies import install_original_http
from tools.browser_production_live_gate import URL, run_case
from tools.completion_artifact_audit import tool_receipts
from tools.completion_explanation_ab import Target

GRANTS = frozenset({"control", "web.read", "browser.observe", "browser.interact"})
FIXTURE = Path(__file__).parent / "fixtures" / "hybrid_omitted_navigation_20261115.json"


@pytest.mark.parametrize("kind", ["chat", "hybrid"])
@pytest.mark.parametrize("native", [False, True])
@pytest.mark.parametrize("task,expected", [
    (f"打开 {URL}。通过 codemode 的 tools.browser_navigate、browser_snapshot 完成操作。", True),
    (f"Navigate to {URL}", True),
    (f"Use browser_navigate to open {URL}", True),
    (f"Open <{URL}>, use browser_navigate", True),
    (f"导航到 {URL}", True),
    (f"读取 {URL}", False),
    (f"打开 {URL}。不要调用 browser_navigate。", False),
    (f"打开 {URL}。Do not call browser_navigate.", False),
    (f"Navigate to {URL}. Do not call browser_navigate.", False),
    (f"Navigate to <{URL}>, do not call browser_navigate.", False),
    (f"Do not call browser_navigate. Navigate to {URL}", True),
    (f"Navigate to {URL}. Do not call browser_navigate. Please use browser_navigate.", True),
    (f"打开 {URL}。示例：\"browser_navigate\"。", False),
    (f"打开 {URL}。`browser_navigate` 是代码示例。", False),
    (f"打开 {URL}。\n> browser_navigate", False),
    (f"Do not navigate to {URL}", False),
    (f"Navigate to https://example.com/other. Do not visit https://example.com/other. Read {URL}", False),
    (f"打开 {URL}。browser_navigate。不要调用 browser_navigate。", False),
    (f"Navigate to {URL} and https://example.com/another", False),
])
def test_ordinary_navigation_uses_explicit_intent_and_the_restored_contract(
    kind: str, native: bool, task: str, expected: bool,
) -> None:
    session = TaskSession(TaskPolicy(GRANTS), task_kind=kind, task_text=task,
                          required_source_urls=explicit_web_intent(task).urls)
    snapshot = restore_snapshot(snapshot_description(build_turn_snapshot(session, native=native)))
    call = initial_navigation(session, snapshot)
    assert (call is not None) is expected
    if call is not None:
        assert call.name == "browser_navigate" and call.args == {"url": URL}


@pytest.mark.parametrize("state", ["denied", "research", "strict", "ambiguous", "attempted", "copy"])
def test_ordinary_navigation_does_not_override_authorization_or_existing_work(state: str) -> None:
    task = f"Navigate to {URL}"
    session = TaskSession(TaskPolicy(GRANTS), task_kind="hybrid", task_text=task,
                          required_source_urls=explicit_web_intent(task).urls)
    if state == "denied":
        session.controller_denied = ("browser_navigate",)
    elif state == "research":
        session.task_kind = "research"
    elif state == "strict":
        session.policy = replace(session.policy, strict_research=True)
    elif state == "ambiguous":
        session.task_text += " and https://example.com/another"
        session.required_source_urls = explicit_web_intent(session.task_text).urls
    elif state == "attempted":
        session._memory_results["navigation"] = ToolResult(ToolCall("browser_navigate", {"url": URL}),
                                                           "failed", ok=False)
    else:
        session.task_text = f"打开 {URL}，把运单号原样写入 dispatch.json 的 carrier_label，保留前导零。browser_navigate"
        session.policy = replace(session.policy, grants=GRANTS | {"project.write", "browser.effect"})
    call = initial_navigation(session, build_turn_snapshot(session))
    if state in {"research", "strict"}:
        assert call is not None and call.name == "browser_navigate" and call.args == {"url": URL}
    else:
        assert call is None


@pytest.mark.usefixtures("public_source_dns", "scripted_local_api_connection", "no_external_advisor_models")
@pytest.mark.parametrize("explicit_open", [False, True])
def test_original_hybrid_replies_keep_navigation_but_cannot_edit_with_an_invalid_read_receipt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, explicit_open: bool,
) -> None:
    fixture = parse_json_object(FIXTURE.read_text(encoding="utf-8"))
    raw_replies, raw_digests = fixture["replies"], fixture["reply_sha256"]
    assert isinstance(raw_replies, list) and isinstance(raw_digests, list)
    replies = [require_json_string(reply, "saved reply") for reply in raw_replies]
    assert [hashlib.sha256(reply.encode()).hexdigest() for reply in replies] == raw_digests
    install_original_http(monkeypatch, replies)
    directory = tmp_path / "replay"
    row = run_case(directory, "hybrid", Target("http://scripted.test/v1", "scripted-fixture", 123456789, 10),
        declare_required_tools=False, restore_snapshots=True, max_turns=len(replies),
        additional_required_tools=(require_tool_success("open_url"),) if explicit_open else ())
    assert row["passed"] is False and row["stop_reason"] == "max_turns", row
    restored = row["restored_snapshots"]
    assert isinstance(restored, int) and restored > 0
    receipts = tool_receipts(directory / "state")
    navigation, = [receipt for receipt in receipts if receipt.result.call.name == "browser_navigate"]
    assert navigation.result.call.call_id == "required-source:navigation" and navigation.result.ok
    assert row["navigations"] == ([URL, URL] if explicit_open else [URL])
    assert len([receipt for receipt in receipts if receipt.result.call.name == "open_url"]) == int(explicit_open)
    for name in ("browser_click", "browser_read"):
        selected = [receipt for receipt in receipts if receipt.result.call.name == name]
        assert len(selected) == 1 and selected[0].result.ok, name
    reads = [receipt.result for receipt in receipts if receipt.result.call.name == "read_file"]
    assert len(reads) == 1 and reads[0].ok and reads[0].call.args == {"path": "result.json"}
    assert not any(receipt.result.call.name == "edit" for receipt in receipts)
    parents = [receipt.result for receipt in receipts if receipt.result.call.name == "codemode"]
    assert not parents[-1].ok
    assert "JSON field edit rejected: use an original complete tools.read_file result" in parents[-1].model_text
    assert row["project_result"] == {"amount": 0, "code": "unknown"}
    assert row["first_rejection"] is None
    rejection = decode_json_object(row["first_tool_rejection"])
    assert rejection["tool"] == "codemode" and rejection["ok"] is False
    assert rejection["model_text"] == parents[-1].model_text
    events = [parse_json_object(line) for line in (directory / "provider.jsonl").read_text(encoding="utf-8").splitlines()]
    first = next(decode_json_object(event["payload"]) for event in events
                 if event.get("type") == "wire_attempt" and event.get("phase") == "request")
    assert navigation.identity in str(first)
    request = parse_json_object((directory / "request.json").read_text(encoding="utf-8"))
    checks = require_json_strings(request["required_tools"], "required tools")
    assert [check for check in checks if (rule := parse_tool_success_check(check)) is None or not rule.file_path
            ] == ([require_tool_success("open_url")] if explicit_open else [])
    assert len([rule for check in checks if (rule := parse_tool_success_check(check)) is not None and rule.file_path]) == 2
