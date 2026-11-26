"""Retry observations commit with decisions, including pre-admission process death."""
from __future__ import annotations

import os
import subprocess
import sys
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path

import pytest

from codey.durable import DurableSession
from codey.durable.model import CommitResult, DurableMutation, thaw_json
from codey.operations.kernel.kernel_driver import load_prepared_request
from codey.operations.kernel_contracts import KernelResult, KernelRunRequest
from codey.operations.kernel_control import validate_decision
from codey.operations.prepared_provider_request import PreparedProviderRequest
from codey.operations.task_effects import KernelEffectSink
from codey.operations.task_loop import run_task_kernel
from codey.operations.task_session import TaskSession
from codey.policies.task_policy import TaskPolicy
from codey.providers.base import AssistantTurn, ProviderToolCall, ProviderToolDefinition, ProviderToolResult, TurnFinish
from codey.providers.requests import SendTextRequest
from codey.runtime.core.operation_payload import RuntimeExecutionCounters, RuntimeOperationState
from codey.runtime.write.checkpoint import operation_from_checkpoint
from codey.runtime.write.mutation_line import RuntimeMutationLine
from codey.types.json_value import JsonObject, decode_json_object
from tests.durable.test_operation_control_actions import request_for as probe_request
from tests.durable.test_operation_execution_counters import _reopen, runtime
from tests.support.kernel_restart import SimulatedExit

request_for: Callable[[DurableSession, KernelEffectSink, object], KernelRunRequest] = probe_request
_ORIGINAL_TASK = "Keep the original task after a cold retry."


def reopen_retry(path: Path) -> tuple[DurableSession, RuntimeMutationLine, KernelEffectSink]:
    durable, line, sink = _reopen(path)
    # These tests reopen inside the same live process. Explicitly take over
    # the abandoned simulated worker instead of waiting for its PID to die.
    durable.recover_scheduler_state(force=True)
    if durable.task("r").status == "pending":
        durable.claim_task("r")
    return durable, line, sink


def run_probe(path: Path, *, mode: str = "text", crash_leaf: str = "", reopen: bool = False,
              hard_exit: bool = False, max_turns: int = 4) -> tuple[KernelResult, list[str], RuntimeExecutionCounters]:
    if reopen:
        durable, _, sink = reopen_retry(path)
    else:
        durable, _, _, sink = runtime(path, sqlite=True, progress_limit=2)
    calls: list[str] = []

    class TextProvider:
        def send(self, prompt: str) -> str:
            if reopen:
                assert _ORIGINAL_TASK in prompt and "Protocol error" in prompt
            calls.append("request")
            return '{"tool": broken'

    class NativeProvider:
        native_tools = True

        def send_turn(self, prompt: str, tools: list[ProviderToolDefinition]) -> AssistantTurn:
            calls.append("request")
            if mode == "length":
                return AssistantTurn(finish=TurnFinish.OUTPUT_LIMIT)
            return AssistantTurn(tool_calls=(ProviderToolCall("bad", "unknown_tool", {}),))

        def send_tool_results(self, results: list[ProviderToolResult], tools: list[ProviderToolDefinition]) -> AssistantTurn:
            return self.send_turn("", tools)

        def acknowledge_tool_results(self, results: list[ProviderToolResult], tools: list[ProviderToolDefinition]) -> AssistantTurn:
            calls.append("close")
            return AssistantTurn()

    def committed(_result: CommitResult, _mutation: DurableMutation) -> None:
        state = operation_from_checkpoint(durable.task("r"), "s")
        assert state is not None
        if state.leaf == crash_leaf and (crash_leaf == "control_ready" or state.logical_turn == 2):
            if hard_exit:
                os._exit(73)
            raise SimulatedExit()

    if crash_leaf:
        durable.subscribe_commits(committed)
    provider = TextProvider() if mode == "text" else NativeProvider()
    request = request_for(durable, sink, provider)
    request = replace(request, transport=replace(request.transport, stagnant_turns=2, user_task=_ORIGINAL_TASK))
    try:
        result = run_task_kernel(TaskSession(TaskPolicy(frozenset({"control"})), max_turns=max_turns), request=request)
        again = run_task_kernel(TaskSession(TaskPolicy(frozenset({"control"})), max_turns=max_turns), request=request)
        assert again == result
        return result, calls, sink.counters()
    finally:
        durable.close()


def saved_retry(path: Path) -> tuple[RuntimeOperationState, JsonObject, PreparedProviderRequest | None]:
    durable, _, sink = reopen_retry(path)
    try:
        state = operation_from_checkpoint(durable.task("r"), "s")
        assert state is not None
        artifact = durable.artifact(state.work_ref)
        ref = decode_json_object(thaw_json(artifact.metadata["ref"]))
        envelope = decode_json_object(sink.managed_outputs.read_json_artifact(dict[str, object](ref)))
        request = load_prepared_request(sink, state.request_ref) if state.request_ref else None
        return state, envelope, request
    finally:
        durable.close()


@pytest.mark.parametrize("mode", ["text", "native", "length"])
@pytest.mark.parametrize("crash_leaf", ["control_ready", "provider_request_ready"])
def test_retry_observation_survives_before_and_after_request_freeze(tmp_path: Path, mode: str, crash_leaf: str) -> None:
    with pytest.raises(SimulatedExit):
        run_probe(tmp_path, mode=mode, crash_leaf=crash_leaf)
    state, envelope, request = saved_retry(tmp_path)
    assert envelope["schema_version"] == 2 and "counters" not in decode_json_object(envelope["data"])
    assert state.counters.invalid_turns == (0 if mode == "length" else 1)
    assert state.counters.length_continuation_used == (mode == "length")
    if request is not None:
        assert request.expected_counters == request.counters == state.counters
        if mode == "text":
            assert isinstance(request.request, SendTextRequest)
            assert _ORIGINAL_TASK in request.request.prompt and "Protocol error" in request.request.prompt
    result, calls, counters = run_probe(tmp_path, mode=mode, reopen=True)
    assert result.stop_reason == ("provider_failure" if mode == "length" else "protocol")
    assert result.turns == 2
    assert calls == (["request", "close"] if mode == "native" else ["request"])
    assert counters.invalid_turns == (0 if mode == "length" else 2)
    assert counters.length_continuation_used == (mode == "length")


@pytest.mark.parametrize("mode", ["text", "length"])
def test_hard_exit_after_retry_commits_the_observed_error(tmp_path: Path, mode: str) -> None:
    script = ("from pathlib import Path; import sys; "
              "from tests.durable.test_retry_observation_commit import run_probe; "
              "run_probe(Path(sys.argv[1]), mode=sys.argv[2], crash_leaf='control_ready', hard_exit=True)")
    child = subprocess.run([sys.executable, "-c", script, str(tmp_path), mode], capture_output=True, text=True, timeout=30)
    assert child.returncode == 73, child.stderr
    state, _, _ = saved_retry(tmp_path)
    assert state.counters.invalid_turns == (1 if mode == "text" else 0)
    assert state.counters.length_continuation_used == (mode == "length")
    result, calls, _ = run_probe(tmp_path, mode=mode, reopen=True)
    assert result.turns == 2 and calls == ["request"]


@pytest.mark.parametrize("schema", [0, 1, 3, True, "2"])
def test_decision_requires_current_version(schema: object) -> None:
    with pytest.raises(ValueError, match="schema"):
        validate_decision({"purpose": "retry", "prompt": "repair", "error": ""}, schema_version=schema)


def test_retry_decision_rejects_an_embedded_counter_copy() -> None:
    payload: dict[str, object] = {"purpose": "retry", "prompt": "repair", "error": ""}
    assert validate_decision(payload, schema_version=2) == payload
    with pytest.raises(ValueError, match="retry"):
        validate_decision({**payload, "counters": RuntimeExecutionCounters(invalid_turns=1).to_payload()})
    with pytest.raises(ValueError, match="schema"):
        validate_decision({**payload, "counters": RuntimeExecutionCounters(invalid_turns=1).to_payload()}, schema_version=1)


@pytest.mark.parametrize("mode", ["text", "length"])
def test_new_retry_records_observation_even_without_a_next_request(tmp_path: Path, mode: str) -> None:
    result, calls, counters = run_probe(tmp_path, mode=mode, max_turns=1)
    assert result.stop_reason == "max_turns" and calls == ["request"]
    assert counters.invalid_turns == (1 if mode == "text" else 0)
    assert counters.length_continuation_used == (mode == "length")
