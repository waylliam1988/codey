"""Experiment boundaries, scoring and original durable browser execution."""
from __future__ import annotations

import json
from collections.abc import Iterator
from dataclasses import replace
from pathlib import Path

import pytest

from codey.automation.browser_session import BrowserSession
from codey.operations.kernel_protocol import build_turn_snapshot, normalize_turn, refresh_snapshot_contract
from codey.operations.provider_receipts import restore_snapshot, snapshot_description
from codey.operations.task_execution import ExecutionDelegate
from codey.operations.task_session import TaskSession
from codey.policies.task_policy import TaskPolicy
from codey.providers.base import AssistantTurn, ProviderToolCall
from codey.runtime.core.models import ToolCall, ToolPlan, ToolResult
from codey.types.json_value import JsonObject, decode_json_object
from tests.test_model_input_binding import copy_snapshot, upload_snapshot
from tests.test_receipt_bound_edit_surface_probe import install_http
from tools import browser_followup_ab as ab
from tools import browser_leaf_exposure_ab as base
from tools.completion_artifact_audit import tool_receipts
from tools.completion_explanation_ab import Target

GRANTS = frozenset({"control", "web.read", "browser.observe", "browser.interact"})


@pytest.mark.parametrize("name", ab.VARIANTS)
def test_variants_reuse_frozen_contract_and_recover(name: str) -> None:
    session = TaskSession(TaskPolicy(GRANTS), task_kind="chat")
    original = build_turn_snapshot(session)
    candidate = ab.expose(original, session, ab.VARIANTS[name])
    assert set(candidate.tool_names) - set(original.tool_names) == set(ab.VARIANTS[name].leaves)
    assert {s.name: s for s in candidate.frozen_specs} == {s.name: s for s in original.frozen_specs}
    assert dict(candidate.custom_executors) == dict(original.custom_executors)
    assert candidate.nested_tool_names == original.nested_tool_names
    assert restore_snapshot(snapshot_description(candidate)) == candidate


def test_program_inputs_preserve_quotes_and_newlines_without_interpolation() -> None:
    session = TaskSession(TaskPolicy(GRANTS), task_kind="chat")
    snapshot = ab.expose(build_turn_snapshot(session), session, ab.VARIANTS["combined"])
    args = {"ref": "observed", "value": base.NOTE + '` ${throw "bad";}'}
    plan = normalize_turn(AssistantTurn(tool_calls=(ProviderToolCall("identity", "browser_fill", args),)), snapshot=snapshot)
    lowered = ab.lower(plan, snapshot, ab.VARIANTS["combined"])
    assert lowered.calls[0].call_id == "identity" and lowered.calls[0].name == "codemode"
    assert lowered.calls[0].args["inputs"] == args
    assert base.NOTE not in str(lowered.calls[0].args["code"])
    assert "browser_snapshot" in str(lowered.calls[0].args["code"])
    assert "browser_click" not in str(lowered.calls[0].args["code"])


@pytest.mark.parametrize("guard", ["grant", "controller", "allowed", "hidden", "upload", "copy", "invalid"])
def test_followups_do_not_bypass_authorization_or_bound_inputs(guard: str) -> None:
    session = TaskSession(TaskPolicy(GRANTS - {"browser.observe"} if guard == "grant" else GRANTS), task_kind="chat")
    if guard == "controller":
        session.controller_denied = ("browser_read",)
    if guard == "hidden":
        session.model_tool_names = ("codemode", "done")
    snapshot = ab.expose(build_turn_snapshot(session), session, ab.VARIANTS["combined"])
    if guard == "allowed":
        snapshot = refresh_snapshot_contract(replace(snapshot, allowed=("browser_click", "codemode", "done")))
    if guard in {"upload", "copy"}:
        snapshot = upload_snapshot() if guard == "upload" else copy_snapshot()
    call = ToolCall("browser_click", {"ref": "old"})
    plan = ToolPlan([call], None, protocol_error="invalid" if guard == "invalid" else "")
    assert ab.lower(plan, snapshot, ab.VARIANTS["combined"]) == plan


def test_failed_original_admission_is_never_lowered(tmp_path: Path) -> None:
    session = TaskSession(TaskPolicy(GRANTS), task_kind="chat")
    with ab.intervention(tmp_path, ab.VARIANTS["combined"]):
        snapshot = ab.kernel_protocol.build_turn_snapshot(session)
        denied = ab.kernel_protocol.normalize_turn(AssistantTurn(tool_calls=(ProviderToolCall(
            "bad", "browser_fill", {"ref": "x"}),)), snapshot=snapshot)
        assert denied.protocol_error and not denied.calls
    assert not (tmp_path / "lowering.jsonl").exists()


def test_fact_matching_checks_numeric_and_marker_boundaries() -> None:
    assert ab.facts_present("净重为173克，QUARTZ_851。", ("173", "QUARTZ_851"))
    assert not ab.facts_present("1173 QUARTZ_851", ("173", "QUARTZ_851"))
    assert not ab.facts_present("173 QUARTZ_851_fake", ("173", "QUARTZ_851"))


def test_source_or_harness_drift_stops_instead_of_scoring(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(ab, "production_source_digest", lambda: "changed")
    with pytest.raises(RuntimeError, match="restart the WHOLE matrix"):
        ab.assert_stable("original", "anything")


def latest_ref(directory: Path, role: str) -> str:
    rows = [item.result for item in tool_receipts(directory / "state")
            if item.result.ok and item.result.call.name == "browser_snapshot"]
    elements = decode_json_object(rows[-1].canonical["value"])["elements"]
    assert isinstance(elements, list)
    return next(str(row["ref"]) for row in elements if isinstance(row, dict) and row["role"] == role)


@pytest.mark.usefixtures("public_source_dns", "scripted_local_api_connection")
@pytest.mark.parametrize("name", ab.VARIANTS)
def test_full_headless_preserves_values_effect_counts_and_formal_evidence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, name: str,
) -> None:
    directory = tmp_path / name
    variant = ab.VARIANTS[name]

    def action(tool: str, args: JsonObject) -> JsonObject:
        return {"tool": tool, "args": args} if tool in variant.leaves else {"tool": "codemode", "args": {
            "code": f"return unwrap(await tools.{tool}(inputs));", "inputs": args}}

    def proposals() -> Iterator[JsonObject]:
        yield action("browser_snapshot", {})
        yield action("browser_fill", {"ref": latest_ref(directory, "textbox"), "value": base.NOTE})
        if not variant.fill_observe:
            yield action("browser_snapshot", {})
        yield action("browser_click", {"ref": latest_ref(directory, "button")})
        if not variant.click_readback:
            yield action("browser_read", {})
        yield {"tool": "done", "args": {"summary": "备注已受理，回执码 NOTE_624。 [s1]"}}

    install_http(monkeypatch, proposals())
    row = ab.run_variant(directory, base.CASES["quoted"], name, Target("http://scripted.test/v1", "fixture", 1, 10))
    assert row["passed"] and row["operation_passed"], row
    assert row["model_calls"] == 6 - int(variant.fill_observe) - int(variant.click_readback)
    assert row["stale_ref_errors"] == 0 and row["updated_source_cited"]
    assert decode_json_object(row["browser_successes"]) == {
        "browser_snapshot": 2, "browser_fill": 1, "browser_click": 1, "browser_read": 1}
    phases = decode_json_object(row["phases"])
    assert sum(int(decode_json_object(p)["calls"]) for p in phases.values()) == row["model_calls"]
    assert sum(int(decode_json_object(p)["prompt_tokens"]) for p in phases.values()) == row["prompt_tokens"]
    assert decode_json_object(row["page_state"])["clicks"] == 1
    if variant.fill_observe or variant.click_readback:
        requests = [r for r in ab.jsonl(directory / "provider.jsonl") if r.get("phase") == "request"]
        assert "Automatic followup" in json.dumps(requests, ensure_ascii=False)
    # Both action children and followups are restored from the original durable receipts.
    receipts = [r.result for r in tool_receipts(directory / "state")]
    assert sum(r.call.name == "browser_fill" for r in receipts) == 1
    assert sum(r.call.name == "browser_click" for r in receipts) == 1


def test_web_source_does_not_become_knowledge_object_with_guidance(tmp_path: Path) -> None:
    session = TaskSession(TaskPolicy(GRANTS), task_kind="chat")
    with ab.intervention(tmp_path, ab.VARIANTS["reference_guidance"]):
        snapshot = ab.kernel_protocol.build_turn_snapshot(session)
        reply = AssistantTurn(tool_calls=(ProviderToolCall("bad-knowledge", "done", {
            "summary": "已读取网页 [s1]", "knowledge_refs": [{"id": "s1", "revision": 1}]}),))
        plan = ab.kernel_protocol.normalize_turn(reply, snapshot=snapshot)
        assert plan.protocol_error and not plan.calls and plan.control is None


def test_existing_knowledge_choices_reject_web_ids() -> None:
    from codey.operations.chat_knowledge import chat_knowledge_spec
    from codey.toolchain.tool_spec import spec_for_tool, validate_args_with_spec

    original = spec_for_tool("done")
    assert original is not None
    bound = chat_knowledge_spec(original, [{"id": "known-object", "revision": 3}])
    assert not validate_args_with_spec(bound, {"summary": "已核对", "knowledge_refs": [{"id": "known-object", "revision": 3}]})
    assert validate_args_with_spec(bound, {"summary": "网页 [s1]", "knowledge_refs": [{"id": "s1", "revision": 1}]})


@pytest.mark.usefixtures("public_source_dns", "scripted_local_api_connection")
@pytest.mark.parametrize("failure", ["fill", "snapshot", "read"])
def test_failed_followup_preserves_committed_action_and_does_not_replay(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure: str,
) -> None:
    directory = tmp_path / failure
    original = ExecutionDelegate._execute_browser_operation
    snapshots = 0

    def browser(delegate: ExecutionDelegate, resource: BrowserSession, call: ToolCall, *, turn: int, tool_index: int):
        nonlocal snapshots
        if call.name == "browser_snapshot":
            snapshots += 1
        if ((failure == "snapshot" and snapshots == 2 and call.name == "browser_snapshot")
                or failure == "read" and call.name == "browser_read"
                or failure == "fill" and call.name == "browser_fill"):
            failed = ToolResult(call=call, ok=False, model_text="Injected boundary failure")
            return failed, False, None
        return original(delegate, resource, call, turn=turn, tool_index=tool_index)

    def proposals() -> Iterator[JsonObject]:
        yield {"tool": "browser_snapshot", "args": {}}
        if failure == "read":
            yield {"tool": "browser_click", "args": {"ref": latest_ref(directory, "button")}}
        else:
            yield {"tool": "browser_fill", "args": {"ref": latest_ref(directory, "textbox"), "value": base.NOTE}}
        yield {"tool": "done", "args": {"summary": "尚未取得完整结果。"}}

    install_http(monkeypatch, proposals())
    monkeypatch.setattr(ExecutionDelegate, "_execute_browser_operation", browser)
    row = ab.run_variant(directory, base.CASES["click" if failure == "read" else "quoted"], "combined",
                         Target("http://scripted.test/v1", "fixture", 1, 10), max_turns=3)
    assert not row["passed"]
    receipts = [r.result for r in tool_receipts(directory / "state")]
    action = "browser_click" if failure == "read" else "browser_fill"
    assert sum(r.call.name == action for r in receipts) == 1
    assert any(r.call.name == action and r.ok for r in receipts) == (failure != "fill")
    if failure == "fill":
        assert sum(r.call.name == "browser_snapshot" for r in receipts) == 1
    if failure == "read":
        assert decode_json_object(row["page_state"])["clicks"] == 1
        assert not decode_json_object(row["evidence"])["updated_browser_read"]


@pytest.mark.usefixtures("public_source_dns", "scripted_local_api_connection")
def test_changed_controls_are_reobserved_instead_of_remapping_old_ref(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    directory = tmp_path / "replace"
    case = replace(base.CASES["form"], html=base.CASES["form"].html.replace(
        '<input id="value">', '<input id="value" oninput="let b=document.querySelector(\'button\');b.replaceWith(b.cloneNode(true))">'))
    initial_button = ""

    def proposals() -> Iterator[JsonObject]:
        nonlocal initial_button
        yield {"tool": "browser_snapshot", "args": {}}
        initial_button = latest_ref(directory, "button")
        yield {"tool": "browser_fill", "args": {"ref": latest_ref(directory, "textbox"), "value": "福州"}}
        assert latest_ref(directory, "button") != initial_button
        yield {"tool": "browser_click", "args": {"ref": latest_ref(directory, "button")}}
        yield {"tool": "done", "args": {"summary": "净重为173克，校验码 QUARTZ_851。 [s1]"}}

    install_http(monkeypatch, proposals())
    row = ab.run_variant(directory, case, "combined", Target("http://scripted.test/v1", "fixture", 1, 10))
    assert row["passed"] and row["stale_ref_errors"] == 0


@pytest.mark.parametrize("tamper", ["none", "input", "sampling", "leak", "authority"])
def test_matrix_audit_rejects_invalid_comparisons(tmp_path: Path, tamper: str) -> None:
    session = TaskSession(TaskPolicy(GRANTS), task_kind="chat")
    snapshot = snapshot_description(build_turn_snapshot(session))
    runs: list[JsonObject] = []
    for name in ("direct", "fill_observe"):
        directory = tmp_path / name
        directory.mkdir()
        ab.write_json(directory / "input.json", {"task": "changed" if tamper == "input" and name == "direct" else "same"})
        payload: JsonObject = {"model": "fixture", "temperature": 0, "seed": 1, "stream": False}
        if tamper == "sampling" and name == "direct":
            payload["seed"] = 2
        if tamper == "leak":
            payload["content"] = "QUARTZ_851"
        ab.append_json(directory / "provider.jsonl", {"phase": "request", "attempt": 1, "payload": payload})
        ab.append_json(directory / "provider.jsonl", {"type": "response"})
        snap = {**snapshot, "policy": {} if tamper == "authority" and name == "direct" else snapshot["policy"]}
        ab.append_json(directory / "snapshots.jsonl", {"snapshot": snap})
        runs.append({"unit": name, "case": "form", "wording": "original", "repeat": 1,
            "source_stable": True, "bound_before_model": True, "source_digest": "source", "harness_digest": "harness",
            "model_calls": 1, "prompt_tokens": 10, "phases": {"operation": {"calls": 1, "prompt_tokens": 10}}})
    manifest = {"variants": {"direct": {}, "fill_observe": {}}, "cases": ["form"], "wordings": ["original"],
                "repeats": 1, "source_digest": "source", "harness_digest": "harness",
                "target": {"model": "fixture", "seed": 1}}
    if tamper == "none":
        assert ab.audit_matrix(tmp_path, runs, manifest)["same_inputs_and_authority"]
    else:
        with pytest.raises(AssertionError):
            ab.audit_matrix(tmp_path, runs, manifest)
