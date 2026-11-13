"""Frozen action contracts use the existing CodeMode permissions and receipts."""
from __future__ import annotations

import json
from collections.abc import Mapping
from pathlib import Path
from types import SimpleNamespace

import pytest

from codey.codemode.kernel import build_execute_turn_executor
from codey.operations.completion_context import CompletionContext
from codey.operations.completion_gate import evaluate
from codey.operations.completion_output import prepare_completion_output
from codey.operations.kernel_contracts import ToolExecutor
from codey.operations.kernel_execution import execute_turn
from codey.operations.kernel_protocol import build_turn_snapshot, normalize_turn
from codey.operations.provider_receipts import restore_snapshot, snapshot_description
from codey.operations.task_session import TaskSession
from codey.policies.output_requirement import declaration
from codey.policies.output_requirement import evaluate as evaluate_output
from codey.policies.task_policy import TaskPolicy, build_task_policy
from codey.policies.tool_requirement import (
    ToolSuccessRequirement,
    parse_tool_success_check,
    require_structured_actions,
    require_tool_success,
)
from codey.protocols.text_turn import decode_text_turn
from codey.providers.base import AssistantTurn, ProviderToolCall
from codey.runtime.core.models import ToolCall, ToolResult
from codey.task.web_intent import explicit_web_intent
from codey.toolchain.tool_spec import thaw_schema_value
from codey.types.json_value import JsonObject, JsonValue, decode_json_object, parse_json_object
from codey.workspace.revision import WorkspaceRevisionStore
from tests.test_explanation_delivery_contract import commit, explanation_schema
from tools.structured_actions_live_gate import schema as metadata_schema

URL = "https://example.com/Case-A?token=X%2FY#Part-1"
GRANTS = frozenset({"control", "web.read", "browser.observe", "browser.interact", "browser.effect",
                    "project.read", "project.write"})


def session_for(*, grants: frozenset[str] = GRANTS, task: str = f"Open <{URL}>.") -> TaskSession:
    return TaskSession(TaskPolicy(grants, required_checks=(require_structured_actions(),)), task_kind="hybrid",
                       task_text=task, required_source_urls=explicit_web_intent(task).urls)


def action(kind: str, **fields: JsonValue) -> JsonObject:
    return {"tool": "codemode", "args": {"action": {"kind": kind, **fields}}}


def execute_action(session: TaskSession, request: JsonObject, *, turn: int = 1, project: Path | None = None,
                   executors: Mapping[str, ToolExecutor] | None = None) -> ToolResult:
    snapshot = restore_snapshot(snapshot_description(build_turn_snapshot(session)))
    plan = normalize_turn(decode_text_turn(json.dumps(request)), snapshot=snapshot)
    assert not plan.protocol_error, plan
    revisions = WorkspaceRevisionStore(project.parent / "revisions") if project is not None else None
    parent = build_execute_turn_executor(session=session, snapshot=snapshot, run_id="actions", effect_scope="task",
        turn=turn, project_path=project, allow_writes=True, workspace_revision_store=revisions, executors=executors)
    return execute_turn(session, plan.calls, run_id="actions", effect_scope="task", turn=turn,
        project_path=project, snapshot=snapshot, executors={"codemode": parent}, workspace_revision_store=revisions)[0]


def test_opt_in_is_explicit_canonical_and_not_a_capability_grant() -> None:
    check = require_structured_actions()
    requirement = parse_tool_success_check(check)
    assert requirement is not None and requirement.structured_actions
    assert '"input":"actions"' in check and requirement.check_id != "tool_succeeded:codemode"
    assert ToolSuccessRequirement("codemode").declaration == "tool_succeeded:codemode"
    submission = SimpleNamespace(required_tools=(check,))
    policy = build_task_policy(submission, task_kind="chat")
    assert policy.grants == frozenset({"control", "knowledge.read"})
    assert policy.required_checks == (check,)
    for kind, strict in (("research", True), ("project", False), ("hybrid", True)):
        with pytest.raises(ValueError, match="ordinary Chat or Hybrid"):
            build_task_policy(submission, task_kind=kind, strict_research=strict)
    with pytest.raises(ValueError, match="canonical"):
        parse_tool_success_check(check.replace('"actions"', '"other"'))


@pytest.mark.parametrize("native", [False, True])
def test_both_protocols_lower_the_same_frozen_action_and_restore(native: bool) -> None:
    session = session_for()
    snapshot = build_turn_snapshot(session, native=native)
    restored = restore_snapshot(snapshot_description(snapshot))
    assert restored == snapshot and restored.task_navigation_url == URL
    binding = restored.input_binding
    assert binding is not None and binding.kind == "actions"
    spec = next(spec for spec in restored.model_specs if spec.name == "codemode")
    parameters = decode_json_object(thaw_schema_value(dict(spec.parameters)))
    branches = decode_json_object(parameters["action"])["anyOf"]
    assert isinstance(branches, list)
    kinds = {str(decode_json_object(decode_json_object(decode_json_object(branch)["properties"])["kind"])["enum"])
             for branch in branches}
    assert {str([kind]) for kind in ("navigate", "fill", "click", "wait", "read", "edit_json", "eval", "tabs")} <= kinds
    args: JsonObject = {"action": {"kind": "navigate", "target": "task"}}
    payload: JsonObject = {"tool": "codemode", "args": args}
    reply = (AssistantTurn(tool_calls=(ProviderToolCall("nav", "codemode", args),)) if native else
             decode_text_turn(json.dumps(payload)))
    plan = normalize_turn(reply, snapshot=restored)
    assert not plan.protocol_error and len(plan.calls) == 1
    assert "target:\"task\"" in str(plan.calls[0].args["code"])
    calls: list[ToolCall] = []

    def navigate(call: ToolCall) -> ToolResult:
        calls.append(call)
        return ToolResult(call, "opened", ok=True, canonical={"value": {"action": "navigate", "delivery": "delivered",
            "text": "opened", "url": call.args["url"], "page_id": "p", "generation": 1, "http_status": 200,
            "content_type": "text/html", "previous_url": "about:blank"}})

    result = execute_action(session, action("navigate", target="task"), executors={"browser_navigate": navigate})
    assert result.ok, result.model_text
    assert [call.args for call in calls] == [{"url": URL}]


@pytest.mark.parametrize("payload", [action("navigate", url=URL), action("unknown"),
    {"tool": "codemode", "args": {"code": "return 1"}}, action("click", target={"ref": "x", "role": "button"})])
def test_action_shape_cannot_fall_back_to_model_javascript(payload: JsonObject) -> None:
    plan = normalize_turn(decode_text_turn(json.dumps(payload)), snapshot=build_turn_snapshot(session_for()))
    assert plan.protocol_error and not plan.calls


def test_branches_follow_permissions_and_restore_rejects_tampering() -> None:
    session = session_for(grants=GRANTS - {"browser.interact", "browser.effect", "project.write"})
    snapshot = build_turn_snapshot(session)
    assert snapshot.task_navigation_url is None
    for request in (action("navigate", target="task"), action("fill", target={"ref": "x"}, value="x"),
                    action("edit_json", path="x.json", changes={"a": 1})):
        assert normalize_turn(decode_text_turn(json.dumps(request)), snapshot=snapshot).protocol_error
    payload = snapshot_description(build_turn_snapshot(session_for()))
    payload["task_navigation_url"] = None
    with pytest.raises(ValueError, match="frozen permissions"):
        restore_snapshot(payload)
    payload = snapshot_description(build_turn_snapshot(session_for()))
    payload["input_binding"] = None
    with pytest.raises(ValueError, match="input binding"):
        restore_snapshot(payload)
    payload = snapshot_description(build_turn_snapshot(session_for()))
    payload["implementation_id"] = "previous-version"
    assert not restore_snapshot(payload).implementation_compatible


@pytest.mark.parametrize(("count", "truncated"), [(0, False), (2, False), (1, True), (1, False)])
def test_named_control_requires_unique_complete_snapshot(count: int, truncated: bool) -> None:
    session = session_for()
    calls: list[ToolCall] = []

    def snapshot(call: ToolCall) -> ToolResult:
        calls.append(call)
        elements: list[JsonValue] = [{"role": "button", "name": "Load record", "ref": f"p:g:{i}",
                                     "disabled": False, "value": None, "checked": None} for i in range(count)]
        return ToolResult(call, "snapshot", ok=True, canonical={"value": {
            "elements": elements, "elements_truncated": truncated, "page_id": "p", "page_url": URL,
            "generation": 1, "text": "page", "complete": True, "scope": "main_frame_visible",
            "text_truncated": False}})

    def click(call: ToolCall) -> ToolResult:
        calls.append(call)
        return ToolResult(call, "clicked", ok=True, canonical={"value": {"action": "click", "delivery": "delivered",
            "text": "clicked", "ref": call.args["ref"], "observation": "page", "effect_verified": False, "page_url": URL}})

    result = execute_action(session, action("click", target={"role": "button", "name": "Load record"}),
        executors={"browser_snapshot": snapshot, "browser_click": click})
    assert result.ok is (count == 1 and not truncated), result.model_text
    assert [call.name for call in calls] == ["browser_snapshot", *(["browser_click"] if result.ok else [])]
    if count == 2:
        assert "p:g:0" in result.model_text and "p:g:1" in result.model_text
        picked = execute_action(session, action("click", target={"ref": "p:g:1"}), turn=2,
                                executors={"browser_snapshot": snapshot, "browser_click": click})
        assert picked.ok and calls[-1].args == {"ref": "p:g:1"}
        assert sum(call.name == "browser_snapshot" for call in calls) == 1


@pytest.mark.parametrize("delivery,reason", [("not_delivered", "stale_ref"), ("delivered", "observation_failed"),
                                           ("uncertain", "transport_error")])
def test_failure_preserves_original_delivery_and_does_not_guess_ref(delivery: str, reason: str) -> None:
    session = session_for()
    calls: list[ToolCall] = []

    def click(call: ToolCall) -> ToolResult:
        calls.append(call)
        return ToolResult(call, "original browser failure", ok=False,
                          canonical={"value": {"delivery": delivery, "reason": reason}})

    result = execute_action(session, action("click", target={"ref": "observed"}), executors={"browser_click": click})
    assert not result.ok and [call.name for call in calls] == ["browser_click"]
    assert delivery in result.model_text and reason in result.model_text
    assert ("not delivered" in result.model_text if reason == "stale_ref" else "Observe the page" in result.model_text)


@pytest.mark.parametrize("contents", [None, "\n".join(["{}"] * 301), '{"theme":"' + "a" * 16001 + '"}'])
def test_fixed_edit_reports_read_failure_or_incomplete_scope_without_edit(tmp_path: Path, contents: str | None) -> None:
    if contents is not None:
        (tmp_path / "preferences.json").write_text(contents, encoding="utf-8")
    session = session_for()
    result = execute_action(session, action("edit_json", path="preferences.json", changes={"theme": "night"}), project=tmp_path)
    assert not result.ok
    assert not any(row.call.name == "edit" for row in session._memory_results.values())
    assert ("No edit was attempted" in result.model_text if contents is not None else "preferences.json" in result.model_text)
    assert "missing valid" not in result.model_text


def test_unsafe_integer_is_rejected_before_reading(tmp_path: Path) -> None:
    session = session_for()
    payload = action("edit_json", path="preferences.json", changes={"n": 9007199254740993})
    plan = normalize_turn(decode_text_turn(json.dumps(payload)), snapshot=build_turn_snapshot(session))
    assert "safe integers" in plan.protocol_error and not plan.calls and not session._memory_results


def test_omission_reuses_field_observation_without_rewriting_the_draft() -> None:
    check = require_tool_success("read_file", answer_pointer="/token", result_pointer="/fact", omit_from_explanation=True)
    requirement = parse_tool_success_check(check)
    assert requirement is not None and requirement.omit_from_explanation
    schema = explanation_schema()
    props = decode_json_object(schema["properties"])
    props["token"] = {"type": "string"}
    schema["properties"] = props
    session = TaskSession(TaskPolicy(frozenset({"control", "project.read"}), required_checks=(
        declaration("json_explanation", json.dumps(schema)), check)), task_kind="chat")
    commit(session, "TOKEN_74")
    draft = "已读。TOKEN_74"
    output = prepare_completion_output(session, draft, CompletionContext())
    assert parse_json_object(output.text) == {"explanation": draft, "token": "TOKEN_74"}
    failed = evaluate(session, output.text, observations=output.observations)
    assert not failed.complete and "answer_text_forbidden" in failed.followup and draft in failed.followup
    corrected = prepare_completion_output(session, "已读取，保留业务分析。", CompletionContext())
    assert evaluate(session, corrected.text, observations=corrected.observations).complete
    assert len(session.executed) == 1


def test_entry_rejects_contradictory_occurrence_rules() -> None:
    contains = require_tool_success("read_file", answer_pointer="/explanation", result_pointer="/fact", answer_contains=True)
    omit = require_tool_success("read_file", answer_pointer="/token", result_pointer="/fact", omit_from_explanation=True)
    submission = SimpleNamespace(required_tools=(contains, omit), output_format="json_explanation")
    with pytest.raises(ValueError, match="required and forbidden"):
        build_task_policy(submission, task_kind="chat")


@pytest.mark.parametrize(("value", "text", "passed"), [("CHANGED_29", "CHANGED_29", False),
    ("CHANGED_29", "OLD_17", True), (284, "结果284克", False), (284, "结果1284克", True)])
def test_omission_uses_the_current_selected_value(value: JsonValue, text: str, passed: bool) -> None:
    rule = ToolSuccessRequirement("read_file", answer_pointer="/field", result_pointer="/fact", omit_from_explanation=True)
    session = TaskSession(TaskPolicy(frozenset({"control", "project.read"})))
    commit(session, value)
    selected = rule.observe(session.executed, results=session._memory_results)
    answer: JsonObject = {"field": value, "explanation": text}
    assert rule.evaluate_observation(selected, json.dumps(answer)).passed is passed


@pytest.mark.parametrize("text,passed", [("已读取结果。C:\\new 中保留字面 \\n。", True),
    ("https://example.com/production-browser-record", False),
    ("HTTPS://example.com/production_browser_record", False)])
def test_explicit_metadata_schema_checks_decoded_text_without_rewriting(text: str, passed: bool) -> None:
    check = declaration("json_explanation", json.dumps(metadata_schema()))
    answer: JsonObject = {"explanation": text, "page_url": URL, "token": "TOKEN_74"}
    assert evaluate_output(check, json.dumps(answer))[0] is passed


@pytest.mark.parametrize("task", [f"打开 {URL}，上传附件。", f"打开 {URL}，把运单号原样写入 dispatch.json 的 carrier_label，保留前导零。"])
def test_specialized_contracts_cannot_silently_replace_explicit_actions(task: str) -> None:
    session = session_for(task=task)
    session.task_kind = "chat"
    with pytest.raises(ValueError, match="specialized binding"):
        build_turn_snapshot(session)
