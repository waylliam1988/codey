"""Retry observations commit with decisions, including pre-admission process death."""
from __future__ import annotations

import os
import subprocess
import sys
from dataclasses import replace

import pytest

from codey.durable.checkpoint import operation_from_checkpoint
from codey.operations.kernel_context import KernelContext
from codey.operations.kernel_control import validate_decision
from codey.operations.kernel_driver import load_prepared_request
from codey.operations.task_loop import run_task_kernel
from codey.operations.task_session import TaskSession
from codey.policies.task_policy import TaskPolicy
from codey.providers.base import AssistantTurn, ProviderToolCall, TurnFinish
from codey.runtime.core.operation_payload import RuntimeExecutionCounters
from codey.runtime.core.progress import ProgressSnapshot
from tests.durable.test_operation_control_actions import request_for
from tests.durable.test_operation_execution_counters import _reopen
from tests.durable.test_provider_response_receipts import runtime
from tests.support.kernel_restart import SimulatedExit


def reopen_retry(path):
    durable, line, sink = _reopen(path)
    # These tests reopen inside the same live process. Explicitly take over
    # the abandoned simulated worker instead of waiting for its PID to die.
    durable.recover_scheduler_state(force=True)
    if durable.task("r").status == "pending":
        durable.claim_task("r")
    return durable, line, sink


def run_probe(path, *, mode="text", crash_leaf="", reopen=False, hard_exit=False, max_turns=4):
    if reopen:
        durable, _, sink = reopen_retry(path)
    else:
        durable, _, _, sink = runtime(path, sqlite=True)
    calls = []

    class TextProvider:
        def send(self, prompt):
            calls.append("request")
            return '{"tool": broken'

    class NativeProvider:
        native_tools = True

        def send_turn(self, prompt, tools):
            calls.append("request")
            if mode == "length":
                return AssistantTurn(finish=TurnFinish.OUTPUT_LIMIT)
            return AssistantTurn(tool_calls=(ProviderToolCall("bad", "unknown_tool", {}),))

        def send_tool_results(self, results, tools):
            return self.send_turn("", tools)

        def acknowledge_tool_results(self, results, tools):
            calls.append("close")
            return AssistantTurn()

    def committed(*_args):
        state = operation_from_checkpoint(durable.task("r"), "s")
        if state.leaf == crash_leaf and (crash_leaf == "control_ready" or state.logical_turn == 2):
            if hard_exit:
                os._exit(73)
            raise SimulatedExit()

    if crash_leaf:
        durable.subscribe_commits(committed)
    provider = TextProvider() if mode == "text" else NativeProvider()
    request = request_for(durable, sink, provider)
    request = replace(request, transport=replace(request.transport, stagnant_turns=2))
    try:
        result = run_task_kernel(TaskSession(TaskPolicy(frozenset({"control"})), max_turns=max_turns), request=request)
        again = run_task_kernel(TaskSession(TaskPolicy(frozenset({"control"})), max_turns=max_turns), request=request)
        assert again == result
        return result, calls, sink.counters()
    finally:
        durable.close()


def saved_retry(path):
    durable, _, sink = reopen_retry(path)
    try:
        state = operation_from_checkpoint(durable.task("r"), "s")
        artifact = durable.artifact(state.work_ref)
        envelope = sink.managed_outputs.read_json_artifact(dict(artifact.metadata["ref"]))
        request = load_prepared_request(sink, state.request_ref)[0] if state.request_ref else None
        return state, envelope, request
    finally:
        durable.close()


@pytest.mark.parametrize("mode", ["text", "native", "length"])
@pytest.mark.parametrize("crash_leaf", ["control_ready", "provider_request_ready"])
def test_retry_observation_survives_before_and_after_request_freeze(tmp_path, mode, crash_leaf):
    with pytest.raises(SimulatedExit):
        run_probe(tmp_path, mode=mode, crash_leaf=crash_leaf)
    state, envelope, request = saved_retry(tmp_path)
    assert envelope["schema_version"] == 2 and "counters" not in envelope["data"]
    assert state.counters.invalid_turns == (0 if mode == "length" else 1)
    assert state.counters.length_continuation_used == (mode == "length")
    if request is not None:
        assert request["admission"]["expected_counters"] == request["admission"]["counters"] == state.counters.to_payload()
    result, calls, counters = run_probe(tmp_path, mode=mode, reopen=True)
    assert result.stop_reason == ("provider_failure" if mode == "length" else "protocol")
    assert result.turns == 2
    assert calls == (["request", "close"] if mode == "native" else ["request"])
    assert counters.invalid_turns == (0 if mode == "length" else 2)
    assert counters.length_continuation_used == (mode == "length")


@pytest.mark.parametrize("mode", ["text", "length"])
def test_hard_exit_after_retry_commits_the_observed_error(tmp_path, mode):
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


def legacy_writer(monkeypatch, *, committed=False, corrupt=None, checkpoint=None):
    """Write the actual v1 artifact/checkpoint pair, without a new production writer."""
    def retry(self, state, prompt, *, error="", counters=None):
        pending = counters or state.counters
        if corrupt:
            pending = corrupt(pending)
        ref = self.sink.managed_outputs.write_json_artifact(session_id="s", run_id="r", payload={
            "schema_version": 1, "kind": "kernel_decision", "data": {
                "purpose": "retry", "prompt": prompt, "error": error, "counters": pending.to_payload()}})
        artifact = self.sink.managed_outputs.json_artifact_record(ref, owner_task_id="r", owner_effect_id=None,
            metadata={"kind": "kernel_decision"})
        self.sink.mutations.advance_execution("s", "r", expected=state, leaf="control_ready", artifact=artifact,
            counters=checkpoint(pending) if checkpoint else pending if committed else None)
    monkeypatch.setattr(KernelContext, "retry", retry)


@pytest.mark.parametrize("mode", ["text", "native", "length"])
@pytest.mark.parametrize("committed", [False, True])
def test_v1_retry_keeps_pending_or_already_committed_observation(tmp_path, monkeypatch, mode, committed):
    with monkeypatch.context() as patch:
        legacy_writer(patch, committed=committed)
        with pytest.raises(SimulatedExit):
            run_probe(tmp_path, mode=mode, crash_leaf="control_ready")
    before, envelope, _ = saved_retry(tmp_path)
    pending = envelope["data"]["counters"]
    assert envelope["schema_version"] == 1
    assert (before.counters.to_payload() == pending) == committed
    with pytest.raises(SimulatedExit):
        run_probe(tmp_path, mode=mode, reopen=True, crash_leaf="provider_request_ready")
    frozen, _, request = saved_retry(tmp_path)
    assert request["admission"]["expected_counters"] == before.counters.to_payload() == frozen.counters.to_payload()
    assert request["admission"]["counters"] == pending
    result, calls, counters = run_probe(tmp_path, mode=mode, reopen=True)
    assert result.turns == 2
    assert result.stop_reason == ("provider_failure" if mode == "length" else "protocol")
    assert calls == (["request", "close"] if mode == "native" else ["request"])
    assert counters.invalid_turns == (0 if mode == "length" else 2)


@pytest.mark.parametrize("change", [
    lambda c: replace(c, invalid_turns=3),
    lambda c: replace(c, closing_round=1),
    lambda c: replace(c, progress=ProgressSnapshot(idle=1)),
    lambda c: replace(c, length_continuation_used=True),  # Two different observations in one old retry.
])
def test_v1_illegal_pending_observation_is_rejected_before_next_send(tmp_path, monkeypatch, change):
    with monkeypatch.context() as patch:
        legacy_writer(patch, corrupt=change)
        with pytest.raises(SimulatedExit):
            run_probe(tmp_path, crash_leaf="control_ready")
    with pytest.raises(ValueError, match="one pending observation"):
        run_probe(tmp_path, reopen=True)
    durable, _, _ = reopen_retry(tmp_path)
    try:
        assert len(durable.effects("r")) == 1
    finally:
        durable.close()


def test_v1_pending_observation_commits_when_retry_stops_without_a_request(tmp_path, monkeypatch):
    with monkeypatch.context() as patch:
        legacy_writer(patch)
        with pytest.raises(SimulatedExit):
            run_probe(tmp_path, crash_leaf="control_ready", max_turns=1)
    result, calls, counters = run_probe(tmp_path, reopen=True, max_turns=1)
    assert result.stop_reason == "max_turns" and calls == [] and counters.invalid_turns == 1


@pytest.mark.parametrize("schema", [0, 3, True, "2"])
def test_decision_version_is_explicit(schema):
    with pytest.raises(ValueError, match="schema"):
        validate_decision({"purpose": "retry", "prompt": "repair", "error": ""}, schema_version=schema)


def test_retry_schema_never_guesses_whether_counters_are_committed():
    payload = {"purpose": "retry", "prompt": "repair", "error": ""}
    old = {**payload, "counters": RuntimeExecutionCounters(invalid_turns=1).to_payload()}
    assert validate_decision(payload, schema_version=2) == payload
    assert validate_decision(old, schema_version=1) == old
    for decision, schema in ((old, 2), (payload, 1)):
        with pytest.raises(ValueError, match="retry"):
            validate_decision(decision, schema_version=schema)


@pytest.mark.parametrize("checkpoint", [
    lambda c: replace(c, invalid_turns=2),
    lambda c: replace(c, length_continuation_used=True),
])
def test_v1_retry_cannot_reset_an_observation_already_in_checkpoint(tmp_path, monkeypatch, checkpoint):
    with monkeypatch.context() as patch:
        legacy_writer(patch, checkpoint=checkpoint)
        with pytest.raises(SimulatedExit):
            run_probe(tmp_path, crash_leaf="control_ready")
    with pytest.raises(ValueError, match="one pending observation"):
        run_probe(tmp_path, reopen=True)


@pytest.mark.parametrize("mode", ["text", "length"])
def test_new_retry_records_observation_even_without_a_next_request(tmp_path, mode):
    result, calls, counters = run_probe(tmp_path, mode=mode, max_turns=1)
    assert result.stop_reason == "max_turns" and calls == ["request"]
    assert counters.invalid_turns == (1 if mode == "text" else 0)
    assert counters.length_continuation_used == (mode == "length")
