"""Execution, progress, and receipts use structured status, never prose."""

from __future__ import annotations

from unittest import mock

import pytest

from codey.agents.runaway_guard import attempt_record
from codey.operations.kernel_execution import execute_turn
from codey.operations.task_execution import _tool_result
from codey.operations.task_session import TaskSession, turn_effect_id
from codey.policies.task_policy import TaskPolicy
from codey.runtime.core.models import ToolCall, ToolResult
from codey.toolchain.runtime import ToolOutcome


def session():
    return TaskSession(policy=TaskPolicy(grants=frozenset({"control", "project.read", "project.verify"})))


@pytest.mark.parametrize("text", ["ERROR: example in a document", "SKIPPED: quoted text", "NEEDS_OPEN: quoted text"])
def test_successful_output_prefix_never_changes_execution_or_progress(text):
    call = ToolCall("read_file", {"path": "a.txt"}, "c1")
    result = _tool_result(call, ToolOutcome(text, True))
    state = session()
    results = execute_turn(state, [call], executors={call.name: lambda _: result}, run_id="status", turn=1)
    assert state.executed[turn_effect_id("status", 1, 0)]["ok"] is True
    assert attempt_record(call, results[0], turn=1).ok is True
    assert results[0].model_text == text


def test_structured_failure_without_prefix_remains_failure():
    call = ToolCall("read_file", {"path": "a.txt"}, "c1")
    result = _tool_result(call, ToolOutcome("权限不足", False, error_code="permission_denied"))
    state = session()
    results = execute_turn(state, [call], executors={call.name: lambda _: result}, run_id="status", turn=1)
    assert state.executed[turn_effect_id("status", 1, 0)]["ok"] is False
    assert attempt_record(call, results[0], turn=1).ok is False


def test_recovered_failure_is_redelivered_without_becoming_success():
    call = ToolCall("read_file", {"path": "a.txt"}, "c1")
    initial = session()
    result = _tool_result(call, ToolOutcome("ERROR: refused", False))
    original = execute_turn(initial, [call], executors={call.name: lambda _: result}, run_id="status", turn=1)[0]
    restored = session()
    executor = mock.Mock(side_effect=AssertionError("must not re-execute settled result"))
    recovered = execute_turn(restored, [call], executors={call.name: executor}, run_id="status", turn=1,
                             delivered={turn_effect_id("status", 1, 0): original})
    assert restored.executed[turn_effect_id("status", 1, 0)]["ok"] is False
    assert recovered[0].call.call_id == "c1"
    executor.assert_not_called()


def test_unstructured_custom_result_is_a_receipted_error():
    call = ToolCall("read_file", {"path": "a.txt"}, "c1")
    state = session()
    results = execute_turn(state, [call], executors={call.name: lambda _: "ordinary output"}, run_id="status", turn=1)
    assert state.executed[turn_effect_id("status", 1, 0)]["ok"] is False
    assert results[0].call == call
    assert "structured ToolResult" in results[0].model_text


@pytest.mark.parametrize("bad", [None, 0, 1, "true", "false"])
def test_tool_result_requires_an_exact_boolean_status(bad):
    with pytest.raises((TypeError, ValueError)):
        ToolResult(call=ToolCall("read_file", {}), model_text="text", ok=bad)
