"""Production result hints obey task intent, frozen interfaces and recovery priority."""
from __future__ import annotations

import json
from dataclasses import replace

import pytest

from codey.operations.browser.browser_task_handoff import browser_task_handoff, handoff_call, query_result_handoff
from codey.operations.browser.turn_surface import offer_browser_tools
from codey.operations.completion_context import CompletionContext
from codey.operations.kernel.kernel_protocol import normalize_turn
from codey.operations.kernel_facts import project_execution_record
from codey.operations.turn_snapshot import build_turn_snapshot
from codey.policies.tool_requirement import require_tool_success
from codey.protocols.text_turn import decode_text_turn
from codey.runtime.core.models import ToolCall, ToolResult
from codey.types.json_value import decode_json_object, parse_json_object
from tests.test_browser_result_adoption_ab import DOCUMENT, session_with_snapshot


@pytest.mark.parametrize("checks", [(), (require_tool_success("browser_wait", arguments={"condition": "text", "value": "Ready"}),)])
def test_plain_chat_offers_only_task_named_controls_with_optional_ordering(checks: tuple[str, ...]) -> None:
    session = session_with_snapshot(checks=checks)
    original = session._memory_results["snapshot"]
    value = decode_json_object(original.canonical["value"])
    elements = value["elements"]
    assert isinstance(elements, list)
    elements.append({"role": "button", "name": "Download fixture", "ref": "download", "disabled": False})
    session._memory_results["snapshot"] = replace(original, canonical={"value": value})
    snapshot = offer_browser_tools(build_turn_snapshot(session), session)
    snapshot, text = browser_task_handoff(session, snapshot, CompletionContext(browser_document=DOCUMENT), 7)
    assert "When you reach" in text and "Download fixture" not in text
    actions = [decode_json_object(parse_json_object(line)["call"]) for line in text.splitlines() if line.startswith("{")]
    assert len(actions) == 1
    assert not normalize_turn(decode_text_turn(json.dumps(actions[0])), snapshot=snapshot).protocol_error


def test_chinese_task_can_name_an_english_control_without_spaces() -> None:
    session = session_with_snapshot(checks=())
    session.task_text = "先填写城市，再点击Details，读取记录。"
    snapshot = offer_browser_tools(build_turn_snapshot(session), session)
    assert query_result_handoff(session, snapshot, CompletionContext(browser_document=DOCUMENT), 7)[1]
    session.task_text = "Click OtherDetails for another task."
    assert not query_result_handoff(session, snapshot, CompletionContext(browser_document=DOCUMENT), 7)[1]


@pytest.mark.parametrize("via", ["direct", "codemode"])
def test_handoff_admits_the_actual_composed_interface(via: str) -> None:
    session = session_with_snapshot()
    session.task_text = "Use codemode only: Click Details." if via == "codemode" else "Click Details."
    snapshot = offer_browser_tools(build_turn_snapshot(session), session)
    # CodeMode-only tasks preserve their original atomic click interface.
    if via == "codemode":
        snapshot = replace(snapshot, browser=replace(snapshot.browser, followups=True))
    offered, action, error = handoff_call(session, snapshot, ToolCall("browser_click", {"ref": "current-one", "purpose": "result"}))
    assert not error and action is not None
    assert action["tool"] == ("browser_click" if via == "direct" else "codemode")
    assert not normalize_turn(decode_text_turn(json.dumps(action)), snapshot=offered).protocol_error


@pytest.mark.parametrize("state", ["delivered", "uncertain", "download"])
def test_recovery_and_download_handoffs_do_not_offer_another_query(state: str) -> None:
    session = session_with_snapshot()
    if state == "download":
        session.policy = replace(session.policy, required_checks=(require_tool_success("browser_download"),))
    else:
        result = ToolResult(ToolCall("browser_click", {"ref": "previous"}), "Sent", ok=state == "delivered",
            canonical={"value": {"delivery": state}},
            audit={"browser_document": DOCUMENT, "query_baseline": {"document": DOCUMENT}})
        session._memory_results["click"] = result
        project_execution_record(session, "click", result.call, result, ok=result.ok)
    snapshot = offer_browser_tools(build_turn_snapshot(session), session)
    _, text = browser_task_handoff(session, snapshot, CompletionContext(browser_document=DOCUMENT), 7)
    assert "When you reach" not in text
    assert ("download" if state == "download" else "browser_snapshot" if state == "uncertain" else "browser_wait") in text
