"""Deprecated Chat input never replaces receipt truth or weakens other arguments."""
from __future__ import annotations

import json
import subprocess
import sys
from dataclasses import replace

import pytest

from codey.operations.completion_context import CompletionContext
from codey.operations.completion_gate import evaluate
from codey.operations.kernel_facts import project_execution_record
from codey.operations.kernel_protocol import TurnSnapshot, build_turn_snapshot, normalize_turn
from codey.operations.provider_receipts import restore_snapshot, snapshot_description
from codey.operations.task_session import TaskSession
from codey.policies.output_requirement import declaration
from codey.policies.task_policy import TaskPolicy
from codey.policies.tool_requirement import require_tool_success
from codey.protocols.text_turn import TextError, decode_text_turn
from codey.providers.api_chat import decode_turn
from codey.providers.api_responses import decode_exchange
from codey.providers.base import AssistantTurn, ProviderToolCall, TurnFinish, tools_from_specs
from codey.research.ledger import ResearchLedger
from codey.runtime.core.execution_receipt import ExecutionReceipt, execution_receipt
from codey.runtime.core.models import ToolCall, ToolPlan, ToolResult
from codey.toolchain.tool_spec import freeze_spec_parameters, json_contract_text, spec_for_tool
from codey.types.json_value import JsonObject, JsonValue


def legacy_snapshot(snapshot: TurnSnapshot, version: int = 3) -> TurnSnapshot:
    done = spec_for_tool("done")
    assert done is not None
    specs = tuple(freeze_spec_parameters(done) if spec.name == "done" else spec for spec in snapshot.frozen_specs)
    legacy = replace(snapshot, frozen_specs=specs, ignore_runtime_claims=False,
                     contract_text=json_contract_text(tuple(spec for spec in specs if spec.name in snapshot.tool_names)))
    payload = snapshot_description(legacy)
    payload["schema_version"] = version
    payload.pop("ignore_runtime_claims")
    if version < 3:
        payload.pop("research_scope")
    if version < 2:
        payload.pop("nested_tool_names")
    return restore_snapshot(payload)


def proposal(args: JsonObject, snapshot: TurnSnapshot, native: bool) -> ToolPlan:
    return normalize_turn(AssistantTurn(tool_calls=(ProviderToolCall("original", "done", args),)) if native else
        decode_text_turn(json.dumps({"tool": "done", "args": args})), snapshot=snapshot)


@pytest.mark.parametrize("format", ["text", "json", "json_explanation"])
def test_chat_contract_removes_only_the_deprecated_input(format: str) -> None:
    check = declaration(format)
    session = TaskSession(TaskPolicy(frozenset({"control"}), required_checks=(check,) if check else ()), task_kind="chat")
    for native in (False, True):
        snapshot = build_turn_snapshot(session, native=native)
        done = next(spec for spec in snapshot.model_specs if spec.name == "done")
        assert snapshot.ignore_runtime_claims
        assert "source_gaps" not in dict(done.parameters)
        assert "source_gaps" not in snapshot.contract_text
        assert {"report_sections", "knowledge_refs"}.issubset(dict(done.parameters).keys())
        assert "execution_claims" not in dict(done.parameters)
        assert all("source_gaps" not in str(tool.parameters) and "source_gaps" not in tool.description
                   for tool in tools_from_specs(snapshot.model_specs))
        assert all("execution_claims" not in str(tool.parameters) and "execution_claims" not in tool.description
                   for tool in tools_from_specs(snapshot.model_specs))


@pytest.mark.parametrize("kind,strict", [("hybrid", False), ("project", False), ("evidence_followup", False), ("research", True)])
def test_other_contracts_do_not_gain_compatibility(kind: str, strict: bool) -> None:
    session = TaskSession(TaskPolicy(frozenset({"control"}), strict_research=strict), task_kind=kind)
    snapshot = build_turn_snapshot(session)
    assert not snapshot.ignore_runtime_claims
    assert proposal({"summary": "answer", "source_gaps": 9}, snapshot, False).protocol_error


@pytest.mark.parametrize("native", [False, True])
@pytest.mark.parametrize("gap", [
    [{"reason": "None", "result_ref": "successful"}],
    [{"reason": "missing", "result_ref": "invented"}],
    [{"reason": "failed", "result_ref": "failure"}, {"reason": "None", "result_ref": "successful"}],
    [], {}, "None", 7, 1.5, True, None,
])
def test_chat_ignores_entire_field_and_preserves_business_text(gap: JsonValue, native: bool) -> None:
    session = TaskSession(TaskPolicy(frozenset({"control"}), required_checks=(declaration("json_explanation"),)), task_kind="chat")
    snapshot = restore_snapshot(snapshot_description(build_turn_snapshot(session, native=native)))
    args: JsonObject = {"summary": '\n {"source_gaps":"business data"} \n', "source_gaps": gap}
    plan = proposal(args, snapshot, native)
    assert not plan.protocol_error and plan.control is not None
    assert plan.control_args == {"summary": args["summary"]}
    assert plan.control.body == args["summary"]
    assert "source_gaps" in args


@pytest.mark.parametrize("native", [False, True])
@pytest.mark.parametrize("claim", [[], {}, "None", 7, 1.5, True, None,
    [{"result_ref": "browser", "status": "success", "exit_code_scope": "process", "exit_code": 0}]])
def test_chat_ignores_execution_claims_without_changing_receipt_requirements(claim: JsonValue, native: bool) -> None:
    session = TaskSession(TaskPolicy(frozenset({"control"})), task_kind="chat")
    snapshot = build_turn_snapshot(session, native=native)
    args: JsonObject = {"summary": "answer", "execution_claims": claim, "source_gaps": claim}
    plan = proposal(args, snapshot, native)
    assert not plan.protocol_error and plan.control_args == {"summary": "answer"}
    assert "execution_claims" in args


@pytest.mark.parametrize("native", [False, True])
@pytest.mark.parametrize("state", ["missing", "failed", "running", "browser", "success"])
def test_runtime_claim_omission_does_not_prove_required_process_success(state: str, native: bool) -> None:
    requirement = require_tool_success("run", arguments={"command": "fixture-command"})
    session = TaskSession(TaskPolicy(frozenset({"control", "project.verify"}), required_checks=(requirement,)),
                          task_kind="chat")
    if state != "missing":
        call = ToolCall("browser_eval" if state == "browser" else "run", {"command": "fixture-command"})
        if state in {"failed", "success"}:
            process = subprocess.run([sys.executable, "-c", "raise SystemExit(7)" if state == "failed" else "pass"],
                                     check=False, capture_output=True, text=True)
            receipt = ExecutionReceipt("exit", process.returncode, process.stdout, exit_code_scope="process")
            result = ToolResult(call, receipt.render(), ok=process.returncode == 0,
                                canonical={"execution": receipt.facts()}, audit=receipt.audit_facts())
        elif state == "running":
            receipt = ExecutionReceipt("running", None, "waiting", exit_code_scope="process")
            result = ToolResult(call, receipt.render(), ok=False, canonical={"execution": receipt.facts()})
        else:
            result = ToolResult(call, "browser action succeeded", ok=True, canonical={"value": 0})
            assert execution_receipt(result) is None
        session._memory_results["receipt"] = result
        project_execution_record(session, "receipt", call, result, ok=result.ok)
    snapshot = restore_snapshot(snapshot_description(build_turn_snapshot(session, native=native)))
    plan = proposal({"summary": "Reported result", "execution_claims": [{"result_ref": "receipt", "status": "exit",
        "exit_code_scope": "process", "exit_code": 0}], "source_gaps": []}, snapshot, native)
    assert not plan.protocol_error
    session.last_done_args = dict(plan.control_args)
    assert evaluate(session, "Reported result").complete is (state == "success")
    if state == "failed":
        session.policy = replace(session.policy, required_checks=())
        assert evaluate(session, "The command failed with observed exit code 7.").complete


@pytest.mark.parametrize("version", [1, 2, 3])
@pytest.mark.parametrize("native", [False, True])
def test_old_snapshots_still_validate_false_process_claims(version: int, native: bool) -> None:
    session = TaskSession(TaskPolicy(frozenset({"control"})), task_kind="chat")
    snapshot = legacy_snapshot(build_turn_snapshot(session, native=native), version)
    plan = proposal({"summary": "answer", "execution_claims": [{"result_ref": "browser", "status": "success",
        "exit_code_scope": "process"}]}, snapshot, native)
    assert not plan.protocol_error
    session.last_done_args = dict(plan.control_args)
    verdict = evaluate(session, "answer")
    assert not verdict.complete and "Eligible shell/process receipts: []" in verdict.followup


@pytest.mark.parametrize("native", [False, True])
@pytest.mark.parametrize("args", [{"source_gaps": []}, {"summary": 9, "source_gaps": []},
                                  {"summary": "valid", "source_gaps": [], "unknown": True}])
def test_other_argument_errors_are_still_rejected(args: JsonObject, native: bool) -> None:
    snapshot = build_turn_snapshot(TaskSession(TaskPolicy(frozenset({"control"})), task_kind="chat"))
    assert proposal(args, snapshot, native).protocol_error


@pytest.mark.parametrize("source", [
    '{"tool":"done","args":{"summary":"a","summary":"b","source_gaps":[]}}',
    '{"tool":"done","args":{"summary":"a","source_gaps":[],"source_gaps":null}}',
    '{"tool":"done","args":{"summary":"unfinished}',
])
def test_outer_json_is_not_repaired(source: str) -> None:
    assert isinstance(decode_text_turn(source), TextError)


@pytest.mark.parametrize("arguments", [
    '{"summary":"a","summary":"b","source_gaps":[]}',
    '{"summary":"a","source_gaps":[],"source_gaps":null}',
])
def test_native_duplicate_arguments_cannot_be_hidden_by_compatibility(arguments: str) -> None:
    turn, history = decode_turn({"tool_calls": [{"id": "original", "type": "function", "function": {
        "name": "done", "arguments": arguments}}]})
    assert turn.finish is TurnFinish.PROTOCOL_ERROR and history is None
    with pytest.raises(RuntimeError, match="invalid function arguments"):
        decode_exchange({"status": "completed", "output": [{"type": "function_call", "call_id": "original",
            "name": "done", "arguments": arguments}]}, text_only=False, text_decoder=None)


@pytest.mark.parametrize("version", [1, 2, 3])
def test_legacy_snapshots_preserve_claim_validation(version: int) -> None:
    session = TaskSession(TaskPolicy(frozenset({"control"})), task_kind="chat")
    snapshot = legacy_snapshot(build_turn_snapshot(session), version)
    assert not snapshot.ignore_runtime_claims
    assert "source_gaps" in snapshot.contract_text
    plan = proposal({"summary": "done", "source_gaps": [{"reason": "None", "result_ref": "invented"}]}, snapshot, False)
    assert not plan.protocol_error
    session.last_done_args = dict(plan.control_args)
    verdict = evaluate(session, "done")
    assert not verdict.complete and "failed acquisition" in verdict.followup


@pytest.mark.parametrize("marker", [None, 0, 1, "true", []])
def test_snapshot_compatibility_marker_requires_a_boolean(marker: JsonValue) -> None:
    payload = snapshot_description(build_turn_snapshot(TaskSession(TaskPolicy(frozenset({"control"})), task_kind="chat")))
    assert payload["schema_version"] == 4
    payload["ignore_runtime_claims"] = marker
    with pytest.raises(ValueError, match="boolean"):
        restore_snapshot(payload)


@pytest.mark.parametrize("state,expected", [("missing", "rejected"), ("failure", "blocked"), ("success", "done")])
def test_ignored_input_cannot_hide_actual_required_source_state(state: str, expected: str) -> None:
    url = "https://example.com/source_gaps"
    session = TaskSession(TaskPolicy(frozenset({"control", "web.read"}), sources_open_required=True), task_kind="chat",
                          required_source_urls=frozenset({url}))
    ledger = ResearchLedger()
    if state == "failure":
        session._memory_results["failure"] = ToolResult(ToolCall("open_url", {"url": url}), "HTTP 403", ok=False,
            canonical={"source_failure": {"url": url, "detail": "HTTP 403", "code": "request_failed"}})
    elif state == "success":
        session.record_open(url)
        ledger.record_open(url, url, "Source", "Actual readable source text")
    snapshot = build_turn_snapshot(session)
    plan = proposal({"summary": "answer", "source_gaps": [{"reason": "None", "result_ref": "failure"}]}, snapshot, False)
    session.last_done_args = dict(plan.control_args)
    verdict = evaluate(session, "answer", context=CompletionContext(research_ledger=ledger))
    assert ("done" if verdict.complete else "blocked" if verdict.blocked else "rejected") == expected
