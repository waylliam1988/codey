"""Restart must preserve the budgets of actual provider and tool actions."""
from __future__ import annotations

import subprocess
import sys
from dataclasses import replace
from pathlib import Path

import pytest

from codey.durable import DurableSession, SQLiteDurableStore
from codey.durable.checkpoint import operation_from_checkpoint
from codey.operations.kernel_progress import KernelProgress
from codey.operations.task_effects import KernelEffectSink, KernelRecordedProvider
from codey.operations.task_session import TaskSession
from codey.policies.task_policy import TaskPolicy
from codey.providers.base import AssistantTurn, ProviderToolCall, ProviderToolResult, TurnFinish
from codey.runtime.core.models import ToolCall, ToolResult
from codey.runtime.core.operation_payload import RuntimeExecutionCounters
from codey.runtime.core.operation_state import RuntimeOperationState, RuntimeOperationTransitionError
from codey.runtime.core.progress import ProgressSnapshot
from codey.runtime.effects.effect_records import RuntimeEffectError, RuntimeEffectIntent
from codey.runtime.write.mutation_line import RuntimeMutationLine
from codey.storage.managed_outputs import ManagedOutputStore
from tests.durable.test_provider_response_receipts import runtime


def _reopen(path):
    durable = DurableSession(SQLiteDurableStore(path / "runtime.sqlite"))
    durable.recover_scheduler_state()
    durable.claim_task("r")
    line = RuntimeMutationLine(durable=durable)
    sink = KernelEffectSink(line, session_id="s", run_id="r", provider_id="local", durable=durable,
        managed_outputs=ManagedOutputStore(path))
    return durable, line, sink


def _kernel(path: Path, *, mode: str, reopen: bool = False, crash_at: int = 0):
    from codey.operations.generation_boundary import durable_generation_boundary_callback
    from codey.operations.task_loop import (
        KernelExecutionDeps,
        KernelObservationDeps,
        KernelRunRequest,
        KernelTransportDeps,
        run_task_kernel,
    )

    if reopen:
        durable, line, sink = _reopen(path)
    else:
        durable, line, _, sink = runtime(path, sqlite=True)
    calls = []

    class Provider:
        native_tools = True

        def send_turn(self, prompt, tools):
            calls.append("request")
            if mode == "length":
                return AssistantTurn(finish=TurnFinish.OUTPUT_LIMIT)
            if mode == "progress":
                return AssistantTurn(tool_calls=(ProviderToolCall("read-1", "read_file", {"path": "a.py"}),))
            return AssistantTurn(tool_calls=(ProviderToolCall("bad-1", "unknown_tool", {}),))

        def send_tool_results(self, results, tools):
            calls.append("repair")
            if mode == "progress":
                return AssistantTurn(tool_calls=(ProviderToolCall("read-2", "read_file", {"path": "a.py"}),))
            return AssistantTurn(tool_calls=(ProviderToolCall("bad-2", "unknown_tool", {}),))

        def acknowledge_tool_results(self, results, tools):
            calls.append("close")
            return AssistantTurn()

    if crash_at:
        import os

        def committed(result, mutation):
            if mode == "progress" and sink.counters().progress.idle == 1:
                os._exit(73)
            if (any(a.metadata.get("kind") == "provider_response" for a in mutation.artifacts)
                    and len([effect for effect in durable.effects("r") if effect.category == "provider_send"]) == crash_at):
                os._exit(73)

        durable.subscribe_commits(committed)
    try:
        result = run_task_kernel(TaskSession(TaskPolicy(frozenset({"control", "project.read"})), max_turns=4),
            request=KernelRunRequest(transport=KernelTransportDeps(provider=KernelRecordedProvider(Provider(), sink),
                provider_id="local", run_id="r", stagnant_turns=2), execution=KernelExecutionDeps(
                    executors={"read_file": lambda call: ToolResult(call, "same", ok=True)}),
                observation=KernelObservationDeps(intent_sink=sink, on_boundary=durable_generation_boundary_callback(
                    durable=durable, session_id="s", task_id="r", sink=sink))))
        return result, calls, sink.counters()
    finally:
        durable.close()


@pytest.mark.parametrize("mode,stop,requests", [("length", "provider_failure", []), ("protocol", "protocol", ["close"])])
def test_restart_of_second_response_keeps_original_budget_and_logical_turn(tmp_path, mode, stop, requests):
    script = "from pathlib import Path; import sys; from tests.durable.test_operation_execution_counters import _kernel; _kernel(Path(sys.argv[1]), mode=sys.argv[2], crash_at=2)"
    child = subprocess.run([sys.executable, "-c", script, str(tmp_path), mode], capture_output=True, text=True, timeout=30)
    assert child.returncode == 73, child.stderr
    result, calls, counters = _kernel(tmp_path, mode=mode, reopen=True)
    assert result.stop_reason == stop and result.turns == 2, result
    assert calls == requests
    assert counters.length_continuation_used == (mode == "length")
    assert counters.invalid_turns == (2 if mode == "protocol" else 0)


def test_closing_budget_is_not_renewed_by_process_restart(tmp_path):
    script = r'''
import os, sys
from pathlib import Path
from tests.durable.test_provider_response_receipts import runtime
from codey.operations.task_effects import KernelRecordedProvider
from codey.operations.kernel_transport import _drain_native_budget
from codey.providers.base import AssistantTurn, ProviderToolCall, ProviderToolResult
durable, line, outputs, sink = runtime(Path(sys.argv[1]), sqlite=True)
class Provider:
    def acknowledge_tool_results(self, results, tools):
        return AssistantTurn(tool_calls=(ProviderToolCall('still-open', 'read_file', {'path':'a.py'}),))
def committed(result, mutation):
    if any(a.metadata.get('kind') == 'provider_response' for a in mutation.artifacts) and sink.counters().closing_round == 2:
        os._exit(73)
durable.subscribe_commits(committed)
_drain_native_budget(KernelRecordedProvider(Provider(), sink), [ProviderToolResult('original', 'stopped')])
'''
    child = subprocess.run([sys.executable, "-c", script, str(tmp_path)], capture_output=True, text=True, timeout=30)
    assert child.returncode == 73, child.stderr
    from codey.operations.kernel_transport import NativeBudgetExhausted, _drain_native_budget

    durable, _, sink = _reopen(tmp_path)
    calls = []

    class Provider:
        def acknowledge_tool_results(self, results, tools):
            calls.append(results[0].call_id)
            return AssistantTurn(tool_calls=(ProviderToolCall("still-open", "read_file", {"path": "a.py"}),))

    try:
        assert sink.counters().closing_round == 2
        with pytest.raises(NativeBudgetExhausted):
            _drain_native_budget(KernelRecordedProvider(Provider(), sink), [ProviderToolResult("still-open", "stopped")])
        assert len(calls) == 2
        assert sink.counters().closing_round == 4
        assert len(durable.effects("r")) == 4
    finally:
        durable.close()


def test_progress_snapshot_round_trip_retains_original_batch_and_stopping_decision():
    session = TaskSession(TaskPolicy(frozenset({"control", "project.read"})))
    result = ToolResult(ToolCall("read_file", {"path": "a.py"}), "same", ok=True)
    progress = KernelProgress(2)
    assert not progress.observe([result], session, batch_id="batch-1")
    assert not progress.observe([result], session, batch_id="batch-2")
    before = progress.snapshot()
    restored = KernelProgress(2, snapshot=ProgressSnapshot.from_payload(before.to_payload()))
    assert not restored.observe([result], session, batch_id="batch-2")
    assert restored.snapshot() == before
    assert restored.observe([result], session, batch_id="batch-3")
    stopped = KernelProgress(2, snapshot=ProgressSnapshot.from_payload(restored.snapshot().to_payload()))
    assert stopped.observe([result], session, batch_id="batch-3")
    assert stopped.snapshot() == restored.snapshot()


def test_progress_committed_before_process_death_is_reused_without_counting_the_batch_again(tmp_path):
    script = "from pathlib import Path; import sys; from tests.durable.test_operation_execution_counters import _kernel; _kernel(Path(sys.argv[1]), mode='progress', crash_at=99)"
    child = subprocess.run([sys.executable, "-c", script, str(tmp_path)], capture_output=True, text=True, timeout=30)
    assert child.returncode == 73, child.stderr
    durable, _, sink = _reopen(tmp_path)
    try:
        snapshot = sink.counters().progress
        assert snapshot.idle == 1 and len(snapshot.attempts) == 2
        progress = KernelProgress(2, snapshot=snapshot)
        session = TaskSession(TaskPolicy(frozenset({"control", "project.read"})))
        result = ToolResult(ToolCall("read_file", {"path": "a.py"}), "same", ok=True)
        assert not progress.observe([result], session, batch_id=snapshot.last_batch_id)
        sink.delivery_batch_id = snapshot.last_batch_id
        revision = durable.task("r").revision
        sink.record_progress(snapshot, progress.snapshot())
        assert durable.task("r").revision == revision
        assert progress.observe([result], session, batch_id="next-batch")
    finally:
        durable.close()


def test_same_progress_commit_retries_without_counting_batch_or_revision_again(tmp_path, monkeypatch):
    from codey.durable.errors import CommitConflict
    from codey.runtime.effects.effect_records import RuntimeEffectSettlement
    from codey.runtime.effects.tool_result_delivery import DeliveryBatchIntent, DeliveryBatchItem, compute_batch_digest

    durable, line, _, sink = runtime(tmp_path)
    item = DeliveryBatchItem(tool_index=0, tool_name="read", ref="tool", replay_class="safe")
    batch = DeliveryBatchIntent(batch_id="batch", session_id="s", run_id="r", turn=1, items=(item,),
                                batch_digest=compute_batch_digest((item,)))
    line.begin_tool_batch("s", "r", intents=(RuntimeEffectIntent("tool", "tool_call", "s", "r", turn=1,
        tool_name="read", args_digest="args", replay_class="safe", delivery_batch_id="batch"),), delivery_intent=batch)
    line.settle_tool_effect("s", "r", RuntimeEffectSettlement("tool", "tool_call", "s", "r", status="ok"))
    sink.delivery_batch_id = "batch"
    before = sink.counters().progress
    observed = replace(before, idle=1, last_batch_id="batch")
    commit = durable.store.commit
    first = True

    def lost_result(mutation):
        nonlocal first
        result = commit(mutation)
        if first:
            first = False
            raise CommitConflict("progress committed before the caller saw success")
        return result

    monkeypatch.setattr(durable.store, "commit", lost_result)
    try:
        sink.record_progress(before, observed)
        revision = durable.task("r").revision
        sink.record_progress(before, observed)
        assert durable.task("r").revision == revision
        assert sink.counters().progress == observed
        with pytest.raises(RuntimeOperationTransitionError, match="immutable"):
            sink.record_progress(before, replace(observed, idle=2))
    finally:
        durable.close()


def test_provider_admission_rejects_stale_counter_update_without_creating_effect(tmp_path):
    durable, line, _, sink = runtime(tmp_path)
    before = sink.counters()
    try:
        with pytest.raises(RuntimeEffectError, match="counters changed"):
            line.begin_provider_effect("s", "r", RuntimeEffectIntent("new", "provider_send", "s", "r", turn=1),
                expected_counters=replace(before, invalid_turns=1), counters=replace(before, invalid_turns=2))
        assert durable.effects("r") == ()
        assert sink.counters() == before
    finally:
        durable.close()


@pytest.mark.parametrize("current,proposed", [
    (RuntimeExecutionCounters(length_continuation_used=True), RuntimeExecutionCounters()),
    (RuntimeExecutionCounters(closing_round=2), RuntimeExecutionCounters(closing_round=1)),
    (RuntimeExecutionCounters(), RuntimeExecutionCounters(closing_round=2)),
    (RuntimeExecutionCounters(invalid_turns=1), RuntimeExecutionCounters(invalid_turns=3)),
    (RuntimeExecutionCounters(), RuntimeExecutionCounters(progress=ProgressSnapshot(idle=2))),
])
def test_request_cannot_reset_budgets_or_replace_batch_progress(current, proposed):
    from codey.runtime.core.operation_reducer import admit_request_counters

    with pytest.raises(RuntimeOperationTransitionError):
        admit_request_counters(current, proposed)


@pytest.mark.parametrize("changes", [{"invalid_turns": True}, {"closing_round": 5}, {"length_continuation_used": 1},
                                     {"progress": {"idle": 0}}])
def test_operation_counter_codec_rejects_corruption(tmp_path, changes):
    durable, _, _, _ = runtime(tmp_path)
    try:
        state = operation_from_checkpoint(durable.task("r"), "s")
        payload = state.to_payload()
        payload["counters"] = {**state.counters.to_payload(), **changes}
        assert RuntimeOperationState.from_payload(payload) is None
    finally:
        durable.close()


def test_legacy_active_operation_cannot_silently_receive_new_budgets(tmp_path):
    durable, _, _, _ = runtime(tmp_path)
    try:
        task = durable.task("r")
        payload = operation_from_checkpoint(task, "s").to_payload()
        payload["schema_version"] = 2
        payload.pop("counters")
        legacy = replace(task, state={**task.state, "operation": payload})
        with pytest.raises(RuntimeOperationTransitionError, match="cannot reset budgets"):
            operation_from_checkpoint(legacy, "s")
        payload["leaf"] = "accepted"
        legacy = replace(task, state={**task.state, "operation": payload})
        admitted = operation_from_checkpoint(legacy, "s")
        assert admitted.counters == RuntimeExecutionCounters()
        migrated = replace(task, state={**task.state, "operation": admitted.to_payload()})
        assert operation_from_checkpoint(migrated, "s") == admitted
    finally:
        durable.close()
