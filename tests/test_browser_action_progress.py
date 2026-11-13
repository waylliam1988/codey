"""Receipt comparisons and request guidance through the frozen action boundary."""
from __future__ import annotations

import json
from dataclasses import replace

import pytest

from codey.operations.action_guidance import action_guidance
from codey.operations.kernel_preparation import prepare_kernel_turn
from codey.operations.kernel_progress import KernelProgress
from codey.operations.kernel_protocol import TurnSnapshot, build_turn_snapshot
from codey.operations.progress_comparison import progress_comparison
from codey.operations.provider_receipts import restore_snapshot, snapshot_description
from codey.operations.structured_actions import lower_action, required_action
from codey.operations.task_session import TaskSession
from codey.policies.tool_requirement import ToolSuccessRequirement, require_structured_actions, require_tool_success
from codey.research.ledger import ResearchLedger
from codey.research.ledger_receipts import ledger_observation
from codey.runtime.core.models import ToolCall, ToolResult
from codey.runtime.core.progress import ProgressSnapshot
from codey.toolchain.tool_spec import validate_args_with_spec
from codey.types.json_value import JsonObject, JsonValue, parse_json_object
from tests.test_structured_actions import GRANTS, URL, action, execute_action, session_for


def control_snapshot(ref: str, *, duplicate: bool = False, truncated: bool = False, text: str = "Loaded") -> ToolResult:
    elements: list[JsonValue] = [{"ref": ref, "role": "button", "name": "Load", "value": None,
                                  "disabled": False, "checked": None}]
    if duplicate:
        row = elements[0]
        assert isinstance(row, dict)
        elements.append({**row, "ref": ref + "other"})
    return ToolResult(ToolCall("browser_snapshot", {}), "snapshot " + ref, ok=True, canonical={"value": {
        "elements": elements, "text": text, "elements_truncated": truncated, "text_truncated": False,
        "page_url": URL, "page_id": "page", "generation": sum(ord(char) for char in ref), "complete": not truncated,
        "scope": "main_frame_visible_text_and_named_controls"}})


def click_result(ref: str, *, ok: bool = True, delivery: str = "delivered", text: str = "Loaded") -> ToolResult:
    value: JsonObject = {"action": "click", "ref": ref, "delivery": delivery,
        "observation": "observed" if ok else "unavailable", "effect_verified": False, "page_url": URL}
    if ok:
        value["text"] = text
    return ToolResult(ToolCall("browser_click", {"ref": ref}), "clicked " + ref if ok else "page observation failed",
                      ok=ok, canonical={"value": value})


def structured_click(session: TaskSession, index: int, *, ok: bool = True) -> ToolResult:
    ref = f"g{index}e1"
    session._memory_results[f"snapshot-{index}"] = control_snapshot(ref)
    child = click_result(ref, ok=ok)
    session._memory_results[f"click-{index}"] = child
    args = lower_action({"action": {"kind": "click", "target": {"role": "button", "name": "Load"}}})
    return ToolResult(ToolCall("codemode", args), "parent " + ref, ok=ok,
        canonical={"child_effect_ids": [f"snapshot-{index}", f"click-{index}"]})


@pytest.mark.parametrize("structured", [False, True])
def test_dynamic_refs_do_not_reset_progress_or_prove_effects_absent(structured: bool) -> None:
    session = session_for()
    snapshot = build_turn_snapshot(session)
    progress = KernelProgress()
    for index in range(5):
        parent = structured_click(session, index)
        result = parent if structured else session._memory_results[f"click-{index}"]
        progress.observe([result], session, batch_id=str(index), snapshot=snapshot)
        assert progress.idle == index
        assert not progress.stopped
    assert len({item.call_fp for item in progress.attempts}) == 1
    assert len({item.result_fp for item in progress.attempts}) == 1
    assert "does not establish" in progress.repeat_feedback()
    assert len(progress.attempts) == 5  # selection snapshots are not extra model attempts


def test_readonly_repeats_stop_and_cold_redelivery_preserves_counts() -> None:
    session = session_for()
    snapshot = build_turn_snapshot(session)
    progress = KernelProgress()
    for index in range(3):
        result = control_snapshot(str(index))
        progress.observe([result], session, snapshot=snapshot, batch_id=f"b{index}")
        saved = ProgressSnapshot.from_payload(parse_json_object(json.dumps(progress.snapshot().to_payload())))
        progress = KernelProgress(snapshot=saved)
        progress.observe([result], session, snapshot=restore_snapshot(snapshot_description(snapshot)), batch_id=f"b{index}")
        assert progress.snapshot() == saved
    assert progress.idle == 2 and progress.stopped


@pytest.mark.parametrize("limit", [4, 20])
def test_stagnation_budget_can_exceed_the_attempt_history(limit: int) -> None:
    session = session_for()
    progress = KernelProgress(limit)
    for index in range(limit + 1):
        result = ToolResult(ToolCall("web_search", {"query": str(index)}), "same information", ok=True)
        progress.observe([result], session)
    assert progress.idle == limit and progress.stopped


def test_fill_fallback_text_does_not_reintroduce_ref() -> None:
    session = session_for()
    progress = KernelProgress()
    for index in range(4):
        ref = str(index)
        session._memory_results[ref] = control_snapshot(ref)
        result = ToolResult(ToolCall("browser_fill", {"ref": ref, "value": "city"}), "Filled and verified " + ref,
            ok=True, canonical={"value": {"action": "fill", "ref": ref, "delivery": "verified", "value_length": 4,
                                          "text": "Filled and verified " + ref}})
        progress.observe([result], session)
    assert progress.idle == 3 and len({row.result_fp for row in progress.attempts}) == 1


@pytest.mark.parametrize("duplicate,truncated", [(True, False), (False, True)])
def test_ambiguous_or_partial_control_lists_do_not_merge_call_identity(duplicate: bool, truncated: bool) -> None:
    session = session_for()
    calls: list[JsonObject] = []
    outputs: list[str] = []
    for ref in ("first", "second"):
        session._memory_results[ref] = control_snapshot(ref, duplicate=duplicate, truncated=truncated)
        comparison = progress_comparison(click_result(ref), session._memory_results, None)
        calls.append(comparison.call.args)
        outputs.append(comparison.information)
    assert calls[0] != calls[1] and outputs[0] == outputs[1]


def test_business_changes_and_returning_to_a_previous_value_are_observable() -> None:
    session = session_for()
    progress = KernelProgress()
    for generation in (7, 8, 7):
        result = ToolResult(ToolCall("browser_eval", {"code": "state"}), "rendered", ok=True,
            canonical={"value": {"action": "eval", "value": {"ref": "business", "generation": generation}, "truncated": False}})
        progress.observe([result], session)
        assert progress.idle == 0
    assert len(progress.seen.keys()) == 2


@pytest.mark.parametrize("field,value", [("text", "Changed"), ("delivery", "uncertain"),
    ("observation", "unavailable"), ("effect_verified", True)])
def test_delivery_and_page_observations_keep_their_meaning(field: str, value: JsonValue) -> None:
    first = click_result("ref")
    assert isinstance(first.canonical["value"], dict)
    second = replace(first, canonical={"value": {**first.canonical["value"], field: value}})
    assert progress_comparison(first, {}, None).information != progress_comparison(second, {}, None).information


def test_parent_failure_preserves_settled_effects_and_observation_opportunity() -> None:
    session = session_for()
    snapshot = build_turn_snapshot(session)
    progress = KernelProgress(1)
    for index in range(4):
        parent = structured_click(session, index, ok=False)
        progress.observe([parent], session, snapshot=snapshot)
        assert not progress.stopped
    assert progress.idle == 4
    assert "Observe" in progress.repeat_feedback()
    comparison = progress_comparison(parent, session._memory_results, snapshot)
    assert 'delivered' in comparison.information and 'unavailable' in comparison.information


@pytest.mark.parametrize("structured", [False, True])
def test_effect_read_cycles_remind_without_stopping_on_repeated_observations(structured: bool) -> None:
    session = session_for()
    snapshot = build_turn_snapshot(session)
    progress = KernelProgress()
    ledger = ResearchLedger()
    ledger.record_open(URL, URL, "record", "Loaded")
    for index in range(3):
        parent = structured_click(session, index)
        click = parent if structured else session._memory_results[f"click-{index}"]
        progress.observe([click], session, snapshot=snapshot)
        read = ToolResult(ToolCall("browser_read", {}), "display", ok=True, canonical={
            "research_observation": ledger_observation(ledger, "open", url=URL),
            "read_passages": [{"ref": "passage", "start": 0, "end": 6}]})
        if structured:
            identity = f"read-{index}"
            session._memory_results[identity] = read
            read = ToolResult(ToolCall("codemode", lower_action({"action": {"kind": "read"}})), "parent", ok=True,
                              canonical={"child_effect_ids": [identity]})
        progress.observe([read], session, snapshot=snapshot)
        assert not progress.stopped
        if index:
            reminder = progress.repeat_feedback()
            assert "does not establish" in reminder and "outstanding requirements" in reminder
    assert progress.idle == 4


def test_general_codemode_returns_are_not_interpreted_as_browser_receipts() -> None:
    one = ToolResult(ToolCall("codemode", {"code": "return inputs", "inputs": {}}), 'business A', ok=True,
                     canonical={"value": {"ref": "a", "generation": 1}, "child_effect_ids": []})
    two = replace(one, model_text='business B', canonical={"value": {"ref": "b", "generation": 2}, "child_effect_ids": []})
    assert progress_comparison(one, {}, None).information != progress_comparison(two, {}, None).information


@pytest.mark.parametrize("tool,field", [("browser_wait", "matched"), ("browser_download", "path"),
    ("browser_download", "handle"), ("browser_download", "sha256"), ("browser_eval", "truncated")])
def test_business_results_and_artifacts_retain_differences(tool: str, field: str) -> None:
    result = ToolResult(ToolCall(tool, {}), "display", ok=True, canonical={"value": {field: "first"}})
    other = replace(result, canonical={"value": {field: "second"}})
    assert progress_comparison(result, {}, None).information != progress_comparison(other, {}, None).information


def test_read_windows_compare_visible_text_without_reference_or_timestamp_churn() -> None:
    def read(text: str, start: int, end: int) -> ToolResult:
        ledger = ResearchLedger()
        ledger.record_open(URL, URL, "record", text)
        return ToolResult(ToolCall("browser_read", {"offset": start}), "dynamic display", ok=True, canonical={
            "research_observation": ledger_observation(ledger, "open", url=URL),
            "read_passages": [{"ref": "variable reference", "start": start, "end": end}]})

    first = progress_comparison(read("AAAABBBB", 0, 4), {}, None)
    assert first.information == progress_comparison(read("AAAACCCC", 0, 4), {}, None).information
    assert first.information != progress_comparison(read("AAAABBBB", 4, 8), {}, None).information
    assert first.information != progress_comparison(read("CCCCBBBB", 0, 4), {}, None).information


def required_session(*checks: str) -> tuple[TaskSession, TurnSnapshot]:
    session = session_for()
    session.policy = replace(session.policy, required_checks=(require_structured_actions(), *checks))
    return session, restore_snapshot(snapshot_description(build_turn_snapshot(session)))


@pytest.mark.parametrize("native", [False, True])
def test_pending_calls_are_frozen_validated_and_coalesced(native: bool) -> None:
    session, snapshot = required_session(require_tool_success("browser_eval", via="codemode"),
        require_tool_success("browser_eval", via="codemode", arguments={"code": "window.token"}),
        require_tool_success("browser_wait", via="codemode", arguments={"condition": "text", "value": "ready", "timeout_ms": 700}))
    text = action_guidance(session, snapshot, native=native)
    rows = [parse_json_object(line) for line in text.splitlines() if line.startswith("{")]
    assert len(rows) == 2
    assert snapshot.input_binding is not None
    for row in rows:
        args = row if native else row["args"]
        assert isinstance(args, dict)
        assert not validate_args_with_spec(snapshot.input_binding.model_spec, args)
        assert "tool" not in row if native else row["tool"] == "codemode"
    assert text.count('"window.token"') == 1 and "tools.browser_" not in text


def test_missing_parameters_and_unpublished_actions_are_explicit() -> None:
    session, snapshot = required_session(require_tool_success("browser_wait", via="codemode"))
    text = action_guidance(session, snapshot, native=False)
    assert "condition, value, timeout_ms" in text and "cannot be executed" in text
    assert not any(line.startswith("{") for line in text.splitlines())
    restricted = TaskSession(replace(session.policy, grants=GRANTS - {"browser.observe"}), task_kind="hybrid")
    snapshot = build_turn_snapshot(restricted)
    text = action_guidance(restricted, snapshot, native=False)
    assert "not published" in text


@pytest.mark.parametrize("args,valid", [({"url": URL}, True), ({"url": "https://example.com/other"}, False),
                                       ({"target": "task"}, False)])
def test_navigation_selectors_use_actual_lowered_url(args: JsonObject, valid: bool) -> None:
    _, snapshot = required_session()
    assert snapshot.input_binding is not None
    candidate = required_action(ToolSuccessRequirement("browser_navigate", arguments_json=json.dumps(args)),
                                snapshot.input_binding.model_spec, snapshot.task_navigation_url, snapshot.frozen_specs)
    assert bool(not candidate.error and not candidate.missing) is valid


def test_fixed_lowering_rejects_empty_edits_and_incompatible_leaf_selectors() -> None:
    _, snapshot = required_session()
    assert snapshot.input_binding is not None
    for args in ({"content": "{}"}, {"base_result_ref": "old"}):
        candidate = required_action(ToolSuccessRequirement("edit", arguments_json=json.dumps(args)),
                                    snapshot.input_binding.model_spec, snapshot.task_navigation_url, snapshot.frozen_specs)
        assert candidate.error
    candidate = required_action(ToolSuccessRequirement("browser_eval", arguments_json='{"code":"x","unused":1}'),
                                snapshot.input_binding.model_spec, snapshot.task_navigation_url, snapshot.frozen_specs)
    assert candidate.error
    args: JsonObject = {"action": {"kind": "edit_json", "path": "result.json", "changes": {}}}
    assert not validate_args_with_spec(snapshot.input_binding.model_spec, args)
    with pytest.raises(ValueError, match="at least one"):
        lower_action(args)


@pytest.mark.parametrize("tool,args", [("browser_snapshot", {"kind": "tabs"}),
    ("browser_click", {"target": {"role": "button", "name": "Load"}})])
def test_action_wrapper_fields_cannot_masquerade_as_leaf_argument_selectors(tool: str, args: JsonObject) -> None:
    _, snapshot = required_session()
    assert snapshot.input_binding is not None
    candidate = required_action(ToolSuccessRequirement(tool, arguments_json=json.dumps(args)),
                                snapshot.input_binding.model_spec, snapshot.task_navigation_url, snapshot.frozen_specs)
    assert candidate.error


def test_evidence_status_distinguishes_satisfied_page_changed_and_truncated_receipts() -> None:
    session, snapshot = required_session(require_tool_success("browser_eval", via="codemode", arguments={"code": "record"},
                                                              answer_pointer="/token", result_pointer="/value/token"))
    document: JsonObject = {"url": URL, "loader_id": "document"}

    def evaluate(call: ToolCall) -> ToolResult:
        return ToolResult(call, "value", ok=True, audit={"browser_document": document}, canonical={"value": {
            "action": "eval", "delivery": "delivered", "value": {"token": "value"}, "truncated": False, "text": "value"}})

    parent = execute_action(session, action("eval", code="record"), executors={"browser_eval": evaluate})
    assert parent.ok, parent.model_text
    text = action_guidance(session, snapshot, native=False, browser_document=document)
    assert "tool_execution_missing" not in text and "Calls validated" not in text
    changed = action_guidance(session, snapshot, native=False, browser_document={**document, "loader_id": "other"})
    assert "No matching receipt observes the current browser page" in changed
    child_ref = next(ref for ref, result in session._memory_results.items() if result.call.name == "browser_eval")
    child = session._memory_results[child_ref]
    session._memory_results[child_ref] = replace(child, truncated=True)
    text = action_guidance(session, snapshot, native=False, browser_document=document)
    assert "incomplete" in text and "Calls validated" in text


def test_initial_guidance_uses_actions_without_legacy_json_edit_programs() -> None:
    session, snapshot = required_session(require_tool_success("browser_eval", via="codemode", arguments={"code": "token"}))
    prepared = prepare_kernel_turn(session, user_task="Update result.json", context_text="", task_guidance="",
                                   native=False, snapshot=snapshot)
    assert '"kind": "eval"' in prepared.prompt
    assert "const read" not in prepared.prompt and "call tools.browser_eval" not in prepared.prompt
    assert "rejected before writing" in prepared.prompt
