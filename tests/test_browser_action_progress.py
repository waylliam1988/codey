"""Browser receipt comparisons and progress accounting."""
from __future__ import annotations

import json
from dataclasses import replace

import pytest

from codey.operations.kernel_progress import KernelProgress
from codey.operations.progress_comparison import progress_comparison
from codey.operations.task_session import TaskSession
from codey.policies.task_policy import TaskPolicy
from codey.research.ledger import ResearchLedger
from codey.research.ledger_receipts import ledger_observation
from codey.runtime.core.models import ToolCall, ToolResult
from codey.runtime.core.progress import ProgressSnapshot
from codey.types.json_value import JsonObject, JsonValue, parse_json_object

URL = "https://example.com/Case-A?token=X%2FY#Part-1"


def session_for() -> TaskSession:
    return TaskSession(TaskPolicy(frozenset({"control"})))


def control_snapshot(ref: str, *, duplicate: bool = False, truncated: bool = False, text: str = "Loaded") -> ToolResult:
    elements: list[JsonValue] = [{"ref": ref, "role": "button", "name": "Load", "value": None,
                                  "disabled": False, "checked": None, "context_id": ""}]
    if duplicate:
        row = elements[0]
        assert isinstance(row, dict)
        elements.append({**row, "ref": ref + "other"})
    return ToolResult(ToolCall("browser_snapshot", {}), "snapshot " + ref, ok=True, canonical={"value": {
        "elements": elements, "contexts": [], "text": text, "elements_truncated": truncated, "text_truncated": False,
        "page_url": URL, "page_id": "page", "generation": sum(ord(char) for char in ref), "complete": not truncated,
        "scope": "main_frame_visible_text_and_named_controls"}})


def click_result(ref: str, *, ok: bool = True, delivery: str = "delivered", text: str = "Loaded") -> ToolResult:
    value: JsonObject = {"action": "click", "ref": ref, "delivery": delivery,
        "observation": "observed" if ok else "unavailable", "effect_verified": False, "page_url": URL}
    if ok:
        value["text"] = text
    return ToolResult(ToolCall("browser_click", {"ref": ref}), "clicked " + ref if ok else "page observation failed",
                      ok=ok, canonical={"value": value})


def test_dynamic_refs_do_not_reset_progress_or_prove_effects_absent() -> None:
    session = session_for()
    progress = KernelProgress()
    for index in range(5):
        ref = f"g{index}e1"
        session._memory_results[f"snapshot-{index}"] = control_snapshot(ref)
        progress.observe([click_result(ref)], session, batch_id=str(index))
        assert progress.idle == index
        assert not progress.stopped
    assert len({item.call_fp for item in progress.attempts}) == 1
    assert len({item.result_fp for item in progress.attempts}) == 1
    assert "does not establish" in progress.repeat_feedback()


def test_readonly_repeats_stop_and_cold_redelivery_preserves_counts() -> None:
    session = session_for()
    progress = KernelProgress()
    for index in range(3):
        result = control_snapshot(str(index))
        progress.observe([result], session, batch_id=f"b{index}")
        saved = ProgressSnapshot.from_payload(parse_json_object(json.dumps(progress.snapshot().to_payload())))
        progress = KernelProgress(snapshot=saved)
        progress.observe([result], session, batch_id=f"b{index}")
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
        comparison = progress_comparison(click_result(ref), session._memory_results)
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
    assert progress_comparison(first, {}).information != progress_comparison(second, {}).information


def test_effect_read_cycles_remind_without_stopping_on_repeated_observations() -> None:
    session = session_for()
    progress = KernelProgress()
    ledger = ResearchLedger()
    ledger.record_open(URL, URL, "record", "Loaded")
    for index in range(3):
        ref = f"g{index}e1"
        session._memory_results[f"snapshot-{index}"] = control_snapshot(ref)
        click = click_result(ref)
        progress.observe([click], session)
        read = ToolResult(ToolCall("browser_read", {}), "display", ok=True, canonical={
            "research_observation": ledger_observation(ledger, "open", url=URL),
            "read_passages": [{"ref": "passage", "start": 0, "end": 6}]})
        progress.observe([read], session)
        assert not progress.stopped
        if index:
            reminder = progress.repeat_feedback()
            assert "does not establish" in reminder and "outstanding requirements" in reminder
    assert progress.idle == 4


def test_general_codemode_returns_are_not_interpreted_as_browser_receipts() -> None:
    one = ToolResult(ToolCall("codemode", {"code": "return inputs", "inputs": {}}), 'business A', ok=True,
                     canonical={"value": {"ref": "a", "generation": 1}, "child_effect_ids": []})
    two = replace(one, model_text='business B', canonical={"value": {"ref": "b", "generation": 2}, "child_effect_ids": []})
    assert progress_comparison(one, {}).information != progress_comparison(two, {}).information


@pytest.mark.parametrize("tool,field", [("browser_wait", "matched"), ("browser_download", "path"),
    ("browser_download", "handle"), ("browser_download", "sha256"), ("browser_eval", "truncated")])
def test_business_results_and_artifacts_retain_differences(tool: str, field: str) -> None:
    result = ToolResult(ToolCall(tool, {}), "display", ok=True, canonical={"value": {field: "first"}})
    other = replace(result, canonical={"value": {field: "second"}})
    assert progress_comparison(result, {}).information != progress_comparison(other, {}).information


def test_read_windows_compare_visible_text_without_reference_or_timestamp_churn() -> None:
    def read(text: str, start: int, end: int) -> ToolResult:
        ledger = ResearchLedger()
        ledger.record_open(URL, URL, "record", text)
        return ToolResult(ToolCall("browser_read", {"offset": start}), "dynamic display", ok=True, canonical={
            "research_observation": ledger_observation(ledger, "open", url=URL),
            "read_passages": [{"ref": "variable reference", "start": start, "end": end}]})

    first = progress_comparison(read("AAAABBBB", 0, 4), {})
    assert first.information == progress_comparison(read("AAAACCCC", 0, 4), {}).information
    assert first.information != progress_comparison(read("AAAABBBB", 4, 8), {}).information
    assert first.information != progress_comparison(read("CCCCBBBB", 0, 4), {}).information
