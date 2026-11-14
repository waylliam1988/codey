"""Declared prerequisites use the production kernel, not a model's remembered checklist."""
from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

from codey.operations.browser_task_handoff import initial_navigation
from codey.operations.kernel_protocol import build_turn_snapshot, normalize_turn, offer_browser_tools
from codey.operations.provider_receipts import restore_snapshot, snapshot_description
from codey.operations.task_session import TaskSession
from codey.policies.browser_requirement import require_browser_page_rounds
from codey.policies.task_policy import TaskPolicy
from codey.policies.tool_requirement import require_tool_success
from codey.protocols.text_turn import decode_text_turn
from codey.runtime.core.models import ToolCall, ToolResult
from codey.types.json_value import JsonObject
from tests.test_protocol_surface import TARGET
from tests.test_receipt_bound_edit_surface_probe import install_http
from tools import browser_prerequisite_ab as fixture
from tools.browser_leaf_exposure_ab import response_actions
from tools.browser_production_live_gate import URL, run_case
from tools.completion_artifact_audit import tool_receipts

GRANTS = frozenset({"control", "web.read", "browser.observe", "browser.interact"})


@pytest.mark.parametrize("via", ["", "codemode"])
def test_navigation_is_bound_and_respects_required_parent(via: str) -> None:
    session = TaskSession(TaskPolicy(GRANTS, required_checks=(require_tool_success("browser_navigate", via=via),)),
                          task_kind="chat", task_text=f"Open {URL}", required_source_urls=frozenset({URL}))
    snapshot = build_turn_snapshot(session)
    call = initial_navigation(session, snapshot)
    assert call is not None and call.name == (via or "browser_navigate")
    assert call.args == ({"code": "return unwrap(await tools.browser_navigate(inputs));", "inputs": {"url": URL}}
                         if via else {"url": URL})
    # A previously opened source must not suppress the distinct navigation requirement.
    session._memory_results["source"] = ToolResult(ToolCall("open_url", {"url": URL}), "source", ok=True)
    assert initial_navigation(session, snapshot) == call
    for name in ("browser_fill", "browser_click", "browser_navigate"):
        session._memory_results["interaction"] = ToolResult(ToolCall(name, {}), "previous", ok=False)
        assert initial_navigation(session, snapshot) is None


@pytest.mark.parametrize("failure", ["absent", "ambiguous", "different_url", "denied", "wrong_parent", "research", "ordered"])
def test_initial_navigation_does_not_invent_permission_or_arguments(failure: str) -> None:
    requirement = require_tool_success("browser_navigate",
        via="other" if failure == "wrong_parent" else "",
        arguments={"url": "https://example.com/different"} if failure == "different_url" else {})
    session = TaskSession(TaskPolicy(GRANTS, required_checks=() if failure == "absent" else (requirement,)),
                          task_kind="research" if failure == "research" else "chat",
                          task_text=f"Open {URL}", required_source_urls=frozenset({URL}))
    if failure == "ambiguous":
        session = replace(session, task_text=f"Open {URL} and https://example.com/other",
                          required_source_urls=frozenset({URL, "https://example.com/other"}))
    if failure == "denied":
        session.controller_denied = ("browser_navigate",)
    if failure == "ordered":
        sequence = require_browser_page_rounds(url=URL, condition="text", value="Ready", rounds=2,
                                               fill_name="Place", fill_value="Taipei", click_name="Load")
        session = replace(session, policy=replace(session.policy, required_checks=(requirement, sequence)))
    assert initial_navigation(session, build_turn_snapshot(session)) is None


@pytest.mark.usefixtures("public_source_dns", "scripted_local_api_connection", "no_external_advisor_models")
@pytest.mark.parametrize("via", ["", "codemode"])
def test_first_request_has_formal_navigation_even_when_model_omits_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, via: str,
) -> None:
    done: JsonObject = {"tool": "done", "args": {"summary": fixture.SUMMARY}}
    install_http(monkeypatch, [fixture.code_action(fixture.LOAD + fixture.WAIT + f"return {fixture.READ};"), done])
    directory = tmp_path / "run"
    row = run_case(directory, "chat", TARGET, max_turns=2,
                   additional_required_tools=(require_tool_success("browser_navigate", via=via),))
    assert row["passed"], row
    receipts = tool_receipts(directory / "state")
    nav, = [r for r in receipts if r.result.call.name == "browser_navigate"]
    assert nav.result.ok
    transcript = (directory / "provider.jsonl").read_text(encoding="utf-8")
    first_request = next(line for line in transcript.splitlines() if '"phase": "request"' in line)
    assert nav.identity in first_request
    if via:
        parents = [parent for parent in receipts if parent.result.call.name == "codemode" and parent.result.ok]
        assert any(isinstance(refs := parent.result.canonical.get("child_effect_ids"), list) and nav.identity in refs
                   for parent in parents)


@pytest.mark.parametrize("required,denied", [(False, False), (True, False), (True, True)])
def test_wait_exposure_uses_the_existing_frozen_leaf(required: bool, denied: bool) -> None:
    session = TaskSession(TaskPolicy(GRANTS, required_checks=(require_tool_success("browser_wait"),) if required else ()),
                          task_kind="chat", controller_denied=("browser_wait",) if denied else ())
    snapshot = offer_browser_tools(build_turn_snapshot(session), session)
    restored = restore_snapshot(snapshot_description(snapshot))
    assert restored == snapshot
    assert ("browser_wait" in snapshot.tool_names) is (required and not denied)
    if required and not denied:
        spec = next(s for s in snapshot.frozen_specs if s.name == "browser_wait")
        assert next(s for s in snapshot.model_specs if s.name == "browser_wait") == spec
        for args in ('{"condition":"invented","value":"Ready","timeout_ms":5000}',
                     '{"condition":"text","value":"Ready"}'):
            invalid = decode_text_turn('{"tool":"browser_wait","args":' + args + '}')
            assert normalize_turn(invalid, snapshot=snapshot).protocol_error


@pytest.mark.usefixtures("public_source_dns", "scripted_local_api_connection", "no_external_advisor_models")
@pytest.mark.parametrize("via", ["", "codemode"])
def test_required_wait_needs_no_model_javascript_and_retains_parent_requirement(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, via: str,
) -> None:
    done: JsonObject = {"tool": "done", "args": {"summary": fixture.SUMMARY}}
    install_http(monkeypatch, [fixture.code_action(fixture.LOAD + "return null;"),
        {"tool": "browser_wait", "args": {"condition": "text", "value": "鱼丸", "timeout_ms": 5000}},
        {"tool": "browser_read", "args": {}}, done])
    directory = tmp_path / "run"
    row = run_case(directory, "chat", TARGET, max_turns=4,
                   additional_required_tools=(require_tool_success("browser_wait", via=via),))
    assert row["passed"], row
    receipts = tool_receipts(directory / "state")
    wait, = [r for r in receipts if r.result.call.name == "browser_wait"]
    assert wait.result.ok
    assert any(parent.result.ok and isinstance(refs := parent.result.canonical.get("child_effect_ids"), list)
               and wait.identity in refs for parent in receipts if parent.result.call.name == "codemode")
    actions = response_actions(directory / "provider.jsonl")
    assert any(action["tool"] == "browser_wait" for action in actions)
