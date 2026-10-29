"""Strict JSON failures must explain how to repair code strings, before effects."""

import json
from types import SimpleNamespace

import pytest

from codey.operations.task_session import TaskSession
from codey.policies.task_policy import TaskPolicy
from codey.runtime.core.models import ToolResult
from tests.support.kernel_protocol_test_helpers import normalize_turn
from tests.support.kernel_runtime import (
    KernelExecutionDeps,
    KernelRunRequest,
    KernelTransportDeps,
    run_task_kernel,
)

POLICY = TaskPolicy(grants=frozenset({"control", "project.write"}))
CONTENT = "import unittest\n\nclass TestExample(unittest.TestCase):\n\tpass\n"


def _edit(content=CONTENT):
    return json.dumps({"tool": "edit", "args": {"path": "test_example.py", "content": content}})


@pytest.mark.parametrize("control", ["\n", "\r", "\t"])
@pytest.mark.parametrize("fenced", [False, True])
def test_bare_json_control_characters_are_rejected_and_fenced_examples_are_not_executed(control, fenced):
    text = _edit("first" + control + "second").replace(json.dumps(control)[1:-1], control)
    if fenced:
        text = "```json\n" + text + "\n```"
    plan = normalize_turn(text, policy=POLICY)
    if fenced:
        assert not plan.calls and plan.control is not None
        assert plan.control.body == text and not plan.protocol_error
        return
    assert not plan.calls and plan.control is None
    assert plan.protocol_error_kind == "invalid_json"
    assert "escape" in plan.protocol_error.lower()
    assert r"\n" in plan.protocol_error


def test_escaped_code_string_is_preserved_exactly():
    plan = normalize_turn(_edit(), policy=POLICY)
    assert not plan.protocol_error
    assert plan.calls[0].args["content"] == CONTENT


def test_unnamed_query_fragment_gets_member_repair_not_quote_escaping_advice():
    from codey.protocols.text_turn import TextError, decode_text_turn

    raw = '{"tool":"web_search","args":{"query":"topic A"," " "topic B"}}'
    decoded = decode_text_turn(raw)
    assert isinstance(decoded, TextError)
    assert 'Every object member must be "name":value' in decoded.message
    assert "no implicit string concatenation" in decoded.message
    assert "For embedded JavaScript" not in decoded.message
    repaired = normalize_turn('{"tool":"web_search","args":{"query":"topic A topic B"}}',
                              policy=TaskPolicy(grants=frozenset({"control", "web.read"})))
    assert not repaired.protocol_error and repaired.calls[0].args == {"query": "topic A topic B"}


def test_invalid_json_member_rejects_entire_text_batch():
    valid = _edit()
    invalid = _edit("first\nsecond").replace(r"\n", "\n")
    plan = normalize_turn(valid + "\n" + invalid, policy=POLICY)
    assert plan.protocol_error_kind == "invalid_json"
    assert not plan.calls


def test_oversized_json_integer_rejects_without_crashing_the_kernel():
    plan = normalize_turn('{"tool":"edit","args":{"content":' + "9" * 5000 + '}}', policy=POLICY)
    assert plan.protocol_error_kind == "invalid_json"
    assert not plan.calls and plan.control is None


def test_decoder_recursion_limit_is_a_rejected_turn(monkeypatch):
    def exhausted(_source):
        raise RecursionError("decoder recursion limit")

    monkeypatch.setattr("codey.operations.kernel_protocol.json.loads", exhausted)
    plan = normalize_turn(_edit(), policy=POLICY)
    assert plan.protocol_error_kind == "invalid_json"
    assert not plan.calls and plan.control is None


def test_shared_kernel_repairs_json_without_executing_malformed_reply(tmp_path):
    prompts = []
    executions = []
    malformed = _edit().replace(r"\n", "\n").replace(r"\t", "\t")

    def send(prompt):
        prompts.append(prompt)
        if len(prompts) == 1:
            return malformed
        if len(prompts) == 2:
            assert r"\n" in prompt and "escape" in prompt.lower()
            assert executions == []
            return _edit()
        pytest.fail("the transport fixture must stop after the repaired edit")

    def edit(call):
        executions.append(call)
        (tmp_path / call.args["path"]).write_text(call.args["content"], encoding="utf-8")
        return ToolResult(ok=True, call=call, model_text="created")

    # Stop after execution: this transport fixture supplies no verification
    # evidence and must not pretend a real edited project is complete.
    result = run_task_kernel(
        TaskSession(policy=POLICY, max_turns=2),
        request=KernelRunRequest(
            transport=KernelTransportDeps(provider=SimpleNamespace(send=send), run_id="json-escape-repair"),
            execution=KernelExecutionDeps(executors={"edit": edit}),
        ),
    )
    assert not result.completed and result.stop_reason == "max_turns"
    assert (tmp_path / "test_example.py").read_text(encoding="utf-8") == CONTENT
    assert len(executions) == 1
    assert len(prompts) == 2


def test_text_provider_receives_durable_boundary_input_before_next_generation(tmp_path) -> None:
    from dataclasses import replace

    from codey.durable.model import SubmissionPlacement
    from codey.operations.generation_boundary import durable_generation_boundary_callback
    from tests.durable.test_operation_control_actions import request_for
    from tests.durable.test_provider_response_receipts import runtime

    durable, _, _, sink = runtime(tmp_path)
    prompts, boundaries = [], []
    def send(prompt):
        prompts.append(prompt)
        if len(prompts) == 1:
            return '{"tool":"read_file","args":{"path":"README.md"}}'
        assert "change direction now" in prompt
        return '{"tool":"done","args":{"summary":"finished"}}'
    def read_file(call):
        durable.admit_input(conversation_id="s", request_id="steer", task_id="steer", text="change direction now",
            provider_snapshot={}, placement=SubmissionPlacement.QUEUED, mode="steer")
        return ToolResult(ok=True, call=call, model_text="read")
    boundary = durable_generation_boundary_callback(durable=durable, session_id="s", task_id="r", sink=sink)
    def commit(request):
        boundaries.append(request.kind)
        return boundary(request)
    request = request_for(durable, sink, SimpleNamespace(send=send), executors={"read_file": read_file})
    request = replace(request, observation=replace(request.observation, on_boundary=commit))
    try:
        result = run_task_kernel(TaskSession(policy=TaskPolicy(frozenset({"control", "project.read"})), max_turns=3), request=request)
        assert result.completed, result
        assert boundaries == ["post_tools", "final"]
        assert len(prompts) == 2
        assert durable.task("steer").state["consumed_by_task_id"] == "r"
    finally:
        durable.close()
