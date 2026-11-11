"""Production prose assembly retains original receipts and strict final checks."""
from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import NoReturn

import pytest

from codey.app.api import run_submit_response
from codey.app.headless_runner import HeadlessRequest, run_headless
from codey.app.task_submit import submit_task
from codey.operations.completion_context import CompletionContext
from codey.operations.completion_gate import evaluate
from codey.operations.completion_output import OutputBindingError, prepare_completion_output
from codey.operations.kernel_facts import project_execution_record
from codey.operations.kernel_preparation import prepare_kernel_turn
from codey.operations.provider_receipts import restore_snapshot, snapshot_description
from codey.operations.task_session import TaskSession
from codey.policies.output_requirement import (
    EXPLANATION_GUIDANCE,
    JSON_ANSWER_GUIDANCE,
    declaration,
    guidance,
    submission_fields,
)
from codey.policies.task_policy import TaskPolicy, build_task_policy
from codey.policies.tool_requirement import require_tool_success
from codey.providers.api_provider import ApiProvider
from codey.providers.base import ProviderToolDefinition
from codey.runtime.core.models import ToolCall, ToolResult
from codey.types.json_value import JsonObject, decode_json_object


def session_for(*bindings: str, strict: bool = False) -> TaskSession:
    return TaskSession(policy=TaskPolicy(frozenset({"control", "project.read"}), strict_research=strict,
                                        required_checks=(declaration("json_explanation"), *bindings)), task_kind="chat")


def add_receipt(session: TaskSession, identity: str, value: str, *, ok: bool = True) -> None:
    call = ToolCall("read_file", {"path": "a.txt"}, identity)
    receipt = ToolResult(call, value, ok=ok, canonical={"value": {"text": value}})
    session._memory_results[identity] = receipt
    project_execution_record(session, identity, call, receipt, ok=ok)


@pytest.mark.parametrize("text", [
    '点击 "Load record"。\n\n路径 C:\\draft\\file，173 克。',
    '{"explanation":"this is still raw text","page_url":"model guess"}',
    '```json\n{"wrong":"still raw"}\n```',
    "",
])
def test_explicit_mode_preserves_summary_as_one_string_even_without_receipt_bindings(text: str) -> None:
    session = session_for()
    output = prepare_completion_output(session, text, CompletionContext())
    assert json.loads(output.text) == {"explanation": text}
    assert evaluate(session, output.text, observations=output.observations).complete
    assert session.last_done_text == ""


def test_missing_failed_and_conflicting_receipts_cannot_be_filled_by_model_claims() -> None:
    binding = require_tool_success("read_file", answer_pointer="/code", result_pointer="/text")
    session = session_for(binding)
    for stage in ("missing", "failed", "first", "conflict"):
        if stage != "missing":
            add_receipt(session, stage, "actual" if stage == "first" else "different", ok=stage != "failed")
        output = prepare_completion_output(session, 'code="actual"', CompletionContext())
        verdict = evaluate(session, output.text, observations=output.observations)
        assert verdict.complete is (stage == "first"), (stage, verdict)
        if stage == "first":
            assert json.loads(output.text) == {"code": "actual", "explanation": 'code="actual"'}
        else:
            assert "code" not in json.loads(output.text)


@pytest.mark.parametrize("pointer", ["", "/explanation", "/explanation/child"])
def test_receipt_cannot_overwrite_declared_explanation_destination(pointer: str) -> None:
    session = session_for(require_tool_success("read_file", answer_pointer=pointer, result_pointer="/text"))
    with pytest.raises(OutputBindingError, match="overlaps"):
        prepare_completion_output(session, "original prose", CompletionContext())


def test_frozen_contract_and_restored_policy_keep_prose_semantics() -> None:
    binding = require_tool_success("read_file", answer_pointer="/code", result_pointer="/text")
    session = session_for(binding)
    add_receipt(session, "r1", "REAL-CODE")
    prepared = prepare_kernel_turn(session, user_task="Read and explain", context_text="", task_guidance="",
                                   native=False)
    assert EXPLANATION_GUIDANCE in prepared.prompt
    assert JSON_ANSWER_GUIDANCE not in prepared.prompt
    assert "supply only unbound JSON fields" not in prepared.prompt
    restored = restore_snapshot(snapshot_description(prepared.snapshot))
    assert restored.policy == session.policy
    assert restored.policy is not None
    fresh = TaskSession(policy=TaskPolicy.from_payload(restored.policy.to_payload()), task_kind="chat")
    for identity, receipt in session._memory_results.items():
        fresh._memory_results[identity] = receipt
        project_execution_record(fresh, identity, receipt.call, receipt, ok=receipt.ok)
    assert prepare_completion_output(fresh, 'quoted "text"\nnext', CompletionContext()) == prepare_completion_output(
        session, 'quoted "text"\nnext', CompletionContext())


def test_new_mode_uses_existing_submission_fields_and_rejects_strict_research() -> None:
    fields = submission_fields({"output_format": "json_explanation"})
    policy = build_task_policy(SimpleNamespace(project=None, **fields), task_kind="chat")
    assert policy.required_checks == (declaration("json_explanation"),)
    with pytest.raises(ValueError, match="strict Research"):
        build_task_policy(SimpleNamespace(project=None, strict_research=True, **fields), task_kind="research")
    with pytest.raises(OutputBindingError, match="strict Research"):
        prepare_completion_output(session_for(strict=True), "prose", CompletionContext())


def test_strict_research_rejects_prose_mode_before_reserving_application_state() -> None:
    def state() -> NoReturn:
        raise AssertionError("invalid output mode reached application state")

    with pytest.raises(ValueError, match="strict Research"):
        submit_task("session", None, "explain", 4, False, "local", get_state=state,
                    strict_research=True, output_format="json_explanation")


def test_api_forwards_production_output_mode_and_receipt_bindings() -> None:
    binding = require_tool_success("read_file", answer_pointer="/code", result_pointer="/text")
    submitted: dict[str, object] = {}

    def submit(*_args: object, **kwargs: object) -> str:
        submitted.update(kwargs)
        return "accepted"

    status, _ = run_submit_response({"task": "read and explain", "output_format": "json_explanation",
                                     "required_tools": [binding]}, submit)
    assert status == 200
    assert submitted["output_format"] == "json_explanation"
    assert submitted["required_tools"] == (binding,)


@pytest.mark.parametrize("schema", [
    '{"type":"array","items":{"type":"string"}}',
    '{"type":"object","properties":{"explanation":{"type":"integer"}},"required":["explanation"]}',
    '{"type":"object","properties":{"explanation":{"type":"string"}}}',
])
def test_invalid_prose_destination_schema_is_rejected(schema: str) -> None:
    with pytest.raises(ValueError, match="required string explanation"):
        declaration("json_explanation", schema)


def test_schema_rejection_requests_prose_without_telling_model_to_encode_inner_json() -> None:
    schema = '{"type":"object","properties":{"explanation":{"type":"string","minLength":5}},"required":["explanation"]}'
    session = session_for()
    session.policy = replace(session.policy, required_checks=(declaration("json_explanation", schema),))
    output = prepare_completion_output(session, "x", CompletionContext())
    verdict = evaluate(session, output.text, observations=output.observations)
    assert not verdict.complete
    assert EXPLANATION_GUIDANCE in verdict.followup
    assert JSON_ANSWER_GUIDANCE not in verdict.followup
    assert "application's final output" in guidance(session.policy.required_checks)


class ScriptedProvider(ApiProvider):
    def __init__(self, replies: list[JsonObject], *, native: bool) -> None:
        super().__init__("http://localhost:9/v1", "fixture", native_tools=native)
        self.replies = iter(replies)
        self.calls = 0

    def _generate(self, messages: list[dict[str, object]], tools: list[ProviderToolDefinition] | None = None,
                  *, timeout: float | None = None, checkpoints: list[dict[str, object]] | None = None,
                  tool_call_json: bool = False, tool_call_schema: dict[str, object] | None = None,
                  tool_call_contract: str = "", tool_call_observations: str = "",
                  tool_call_history_groups: int = 0, output_token_limit: int = 0,
                  require_tool_call: bool = False) -> dict[str, object]:
        self.calls += 1
        reply = next(self.replies)
        if self.native_tools:
            message: dict[str, object] = {"role": "assistant", "content": "", "tool_calls": [{
                "id": f"call-{self.calls}", "type": "function", "function": {
                    "name": reply["tool"], "arguments": json.dumps(reply["args"])}}]}
        else:
            message = {"role": "assistant", "content": json.dumps(reply), "tool_calls": []}
        return {"choices": [{"message": message, "finish_reason": "tool_calls" if self.native_tools else "stop"}]}


@pytest.mark.parametrize("native", [False, True])
@pytest.mark.parametrize("mode", ["chat", "hybrid"])
@pytest.mark.parametrize("prose", ['\n  exact "explanation"  \n', '{"source_gaps":"business data"}', ""])
def test_headless_production_publishes_prose_and_receipt_fields_without_provider_adapter(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, native: bool, mode: str, prose: str,
) -> None:
    def availability(*_args: object, **_kwargs: object) -> dict[str, bool]:
        return {"local": True}

    monkeypatch.setattr("codey.app.provider_services.provider_availability", availability)
    project = tmp_path / "project"
    project.mkdir()
    (project / "a.txt").write_text("REAL-CODE\n", encoding="utf-8")
    done_args: JsonObject = {"summary": prose}
    if mode == "chat":
        done_args["source_gaps"] = [{"reason": "None", "result_ref": "invented"}]
    provider = ScriptedProvider([
        {"tool": "codemode", "args": {"code": "return unwrap(await tools.read_file({path:'a.txt'}));"}},
        {"tool": "done", "args": done_args},
    ], native=native)
    events: list[JsonObject] = []

    def observe(value: dict[str, object]) -> None:
        events.append(decode_json_object(value))

    def connect(_provider_id: str, **_kwargs: object) -> ScriptedProvider:
        return provider

    schema = {"type": "object", "properties": {"code": {"type": "string"}, "explanation": {"type": "string"}},
              "required": ["code", "explanation"], "additionalProperties": False}
    result = run_headless(HeadlessRequest(project, "Read a.txt and explain.", provider_id="local", intent=mode,
        project_changes_required=False, max_turns=3, state_home=tmp_path / "state",
        research_store_root=tmp_path / "knowledge", output_format="json_explanation", output_schema=json.dumps(schema),
        required_tools=(require_tool_success("read_file", via="codemode", arguments={"path": "a.txt"},
                                            answer_pointer="/code", result_pointer="/text"),)),
        emit_jsonl=observe, connect_provider=connect)
    terminal = next(row for row in reversed(events) if row["type"] == "task_done")
    assert result.stop_reason == "done", terminal
    assert json.loads(str(terminal["summary"])) == {"code": "REAL-CODE\n", "explanation": prose}
    assert provider.calls == 2
    assert sum(row["type"] == "tool" and row.get("tool_name") == "read_file" for row in events) == 1


@pytest.mark.parametrize("native", [False, True])
def test_optional_report_proposal_cannot_choose_repair_mode(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, native: bool,
) -> None:
    from codey.operations import kernel_prompt

    modes: list[bool] = []
    render = kernel_prompt._completion_followup

    def capture(feedback: str, *, native: bool, structured_report: bool = False,
                whole_submission: bool = False) -> str:
        modes.append(structured_report)
        return render(feedback, native=native, structured_report=structured_report, whole_submission=whole_submission)

    monkeypatch.setattr(kernel_prompt, "_completion_followup", capture)
    monkeypatch.setattr("codey.app.provider_services.provider_availability", lambda: {"local": True})
    provider = ScriptedProvider([
        {"tool": "done", "args": {"summary": "x", "report_sections": {"evidence": ["Unsupported report"],
            "counter": [], "coverage": [{"question_ref": "q1", "state": "not_investigated", "answer": "unknown",
                                       "findings": [], "investigations": []}]}}},
        {"tool": "done", "args": {"summary": "A sufficiently complete explanation."}},
    ], native=native)

    def connect(_provider_id: str, **_kwargs: object) -> ScriptedProvider:
        return provider

    schema = {"type": "object", "properties": {"explanation": {"type": "string", "minLength": 10}},
              "required": ["explanation"], "additionalProperties": False}

    def observe(_row: dict[str, object]) -> None:
        pass

    result = run_headless(HeadlessRequest(None, "Explain the result.", provider_id="local", intent="chat",
        max_turns=3, state_home=tmp_path / "state", research_store_root=tmp_path / "knowledge",
        output_format="json_explanation", output_schema=json.dumps(schema)), connect_provider=connect, emit_jsonl=observe)
    assert result.stop_reason == "done"
    assert modes == [False]
