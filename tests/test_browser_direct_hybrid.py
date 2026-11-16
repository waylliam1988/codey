"""Ordinary Hybrid reuses browser leaves and their production follow-ups."""
from __future__ import annotations

from collections.abc import Iterator
from dataclasses import replace
from pathlib import Path

import pytest

from codey.operations.browser_followups import DIRECT_TOOLS
from codey.operations.kernel_protocol import build_turn_snapshot, normalize_turn, offer_browser_tools
from codey.operations.provider_receipts import restore_snapshot, snapshot_description
from codey.operations.task_session import TaskSession
from codey.policies.task_policy import TaskPolicy
from codey.policies.tool_requirement import require_tool_success
from codey.protocols.text_turn import decode_text_turn
from codey.types.json_value import JsonObject, decode_json_object
from tests.test_model_input_binding import copy_snapshot, upload_snapshot
from tests.test_receipt_bound_edit_surface_probe import install_http
from tools.browser_prerequisite_ab import LOAD, WAIT, code_action
from tools.browser_production_live_gate import run_case
from tools.completion_artifact_audit import artifacts, tool_receipts
from tools.completion_explanation_ab import Target

GRANTS = frozenset({"control", "web.read", "browser.observe", "browser.interact"})


@pytest.mark.parametrize("name", ["browser_snapshot", "browser_read"])
@pytest.mark.parametrize("parent_bound", [False, True])
def test_direct_observation_preserves_the_declared_parent(name: str, parent_bound: bool) -> None:
    session = TaskSession(TaskPolicy(GRANTS, required_checks=(require_tool_success(
        name, via="codemode" if parent_bound else ""),)), task_kind="hybrid")
    snapshot = restore_snapshot(snapshot_description(offer_browser_tools(build_turn_snapshot(session), session)))
    plan = normalize_turn(decode_text_turn('{"tool":"' + name + '","args":{}}'), snapshot=snapshot)
    assert not plan.protocol_error and len(plan.calls) == 1
    assert plan.calls[0].name == ("codemode" if parent_bound else name)


@pytest.mark.parametrize("guard", ["research", "strict", "project", "codemode", "denied", "hidden", "grant", "allowed", "copy", "upload"])
def test_hybrid_shortcuts_preserve_existing_boundaries(guard: str) -> None:
    session = TaskSession(TaskPolicy(GRANTS), task_kind="hybrid")
    if guard in {"research", "project"}:
        session.task_kind = guard
    elif guard == "codemode":
        session.task_text = "Use codemode only."
    elif guard == "denied":
        session.controller_denied = ("browser_read",)
    elif guard == "hidden":
        session.model_tool_names = ("codemode", "done")
    elif guard == "grant":
        session.policy = replace(session.policy, grants=GRANTS - {"browser.observe"})
    snapshot = build_turn_snapshot(session)
    if guard == "strict":
        session.policy = replace(session.policy, strict_research=True)
        snapshot = replace(snapshot, policy=session.policy)
    elif guard == "allowed":
        snapshot = replace(snapshot, allowed=("codemode", "done"))
    elif guard in {"copy", "upload"}:
        snapshot = copy_snapshot() if guard == "copy" else upload_snapshot()
    assert offer_browser_tools(snapshot, session) == snapshot


@pytest.mark.parametrize("guard", ["none", "denied", "hidden", "allowed"])
def test_ordinary_wait_entry_needs_no_checklist_and_preserves_authorization(guard: str) -> None:
    session = TaskSession(TaskPolicy(GRANTS), task_kind="hybrid")
    if guard == "denied":
        session.controller_denied = ("browser_wait",)
    elif guard == "hidden":
        session.model_tool_names = ("codemode", "done", *DIRECT_TOOLS)
    snapshot = build_turn_snapshot(session)
    if guard == "allowed":
        snapshot = replace(snapshot, allowed=("codemode", "done", *DIRECT_TOOLS))
    offered = offer_browser_tools(snapshot, session)
    assert offered.browser_followups and set(DIRECT_TOOLS) <= set(offered.tool_names)
    assert ("browser_wait" in offered.tool_names) is (guard == "none")
    assert restore_snapshot(snapshot_description(offered)) == offered


@pytest.mark.usefixtures("public_source_dns", "scripted_local_api_connection", "no_external_advisor_models")
@pytest.mark.parametrize("declared_wait", [False, True])
@pytest.mark.parametrize("parent_bound", [False, True])
def test_hybrid_fills_refreshes_waits_reads_and_edits_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, declared_wait: bool, parent_bound: bool,
) -> None:
    directory = tmp_path / "run"
    wait: JsonObject = {"condition": "text", "value": "鱼丸", "timeout_ms": 5000}

    def ref(name: str) -> str:
        snapshots = [row.result for row in tool_receipts(directory / "state")
                     if row.result.call.name == "browser_snapshot" and row.result.ok]
        elements = decode_json_object(snapshots[-1].canonical["value"])["elements"]
        assert isinstance(elements, list)
        return next(str(row["ref"]) for row in elements if isinstance(row, dict) and row.get("name") == name)

    def replies() -> Iterator[JsonObject]:
        yield {"tool": "browser_snapshot", "args": {}}
        yield {"tool": "browser_fill", "args": {"ref": ref("Locality"), "value": "福州"}}
        yield {"tool": "browser_click", "args": {"ref": ref("Load record"), **({"purpose": "result"} if declared_wait else {})}}
        if not declared_wait:
            yield {"tool": "browser_wait", "args": wait}
            yield {"tool": "browser_read", "args": {}}
        yield {"tool": "codemode", "args": {"path": "result.json"}}
        yield {"tool": "done", "args": {"summary": "净重 173 克，校验码 QUARTZ_851。"}}

    install_http(monkeypatch, replies())
    result = run_case(directory, "hybrid", Target("http://scripted.test/v1", "fixture", 123456789, 10),
        declare_required_tools=False, restore_snapshots=True, max_turns=8,
        additional_required_tools=(*(require_tool_success(name, via="codemode") for name in (
            ("browser_snapshot", "browser_read") if parent_bound else ())),
            *((require_tool_success("browser_wait", via="codemode", arguments=wait),) if declared_wait else ())))
    assert result["passed"] is True and result["final_file_correct"] is True, result
    calls = [row.result.call.name for row in tool_receipts(directory / "state") if row.result.ok]
    assert {name: calls.count(name) for name in ("browser_fill", "browser_click", "browser_wait", "browser_read", "edit")} == {
        "browser_fill": 1, "browser_click": 1, "browser_wait": 1, "browser_read": 1, "edit": 1}
    assert calls.count("browser_snapshot") == 2
    if parent_bound:
        receipts = tool_receipts(directory / "state")
        for name in ("browser_snapshot", "browser_read"):
            assert any(child.result.call.name == name and child.result.ok and any(
                parent.result.call.name == "codemode" and parent.result.ok
                and isinstance(refs := parent.result.canonical.get("child_effect_ids"), list) and child.identity in refs
                for parent in receipts) for child in receipts)
    requests = [decode_json_object(item["payload"])["args"] for _, item in artifacts(directory / "state")
                if item.get("kind") == "provider_request"]
    assert any(isinstance(args, list) and any(isinstance(value, str) and "prefer the offered direct browser tools" in value
               for value in args) for args in requests)


@pytest.mark.usefixtures("public_source_dns", "scripted_local_api_connection", "no_external_advisor_models")
def test_direct_read_reaches_bound_metadata_without_repeating_browser_work(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    directory = tmp_path / "structured"

    def replies() -> Iterator[JsonObject]:
        yield code_action(LOAD + WAIT + "return null;")
        yield {"tool": "browser_read", "args": {}}
        for _ in range(2):
            requests = [decode_json_object(item["payload"])["snapshot"] for _, item in artifacts(directory / "state")
                        if item.get("kind") == "provider_request"]
            names = decode_json_object(requests[-1])["tool_names"]
            assert isinstance(names, list)
            name = next(name for name in ("browser_eval", "browser_tab_list") if name in names)
            yield {"tool": name, "args": {"code": "window.__contractToken"} if name == "browser_eval" else {}}
        yield {"tool": "done", "args": {"paragraphs": ["已读取当前记录。", "净重 173 克，校验码 QUARTZ_851。"]}}

    install_http(monkeypatch, replies())
    result = run_case(directory, "chat", Target("http://scripted.test/v1", "fixture", 123456789, 10),
                      explanation_output=True, restore_snapshots=True, max_turns=5)
    assert result["passed"] is True, result
    calls = [row.result.call.name for row in tool_receipts(directory / "state") if row.result.ok]
    assert {name: calls.count(name) for name in ("browser_click", "browser_read", "browser_eval", "browser_tab_list")} == {
        "browser_click": 1, "browser_read": 1, "browser_eval": 1, "browser_tab_list": 1}
