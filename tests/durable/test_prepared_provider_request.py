"""One request driver across preparation, admission and real process death."""
from __future__ import annotations

import subprocess
import sys
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from codey.durable import DurableSession, SQLiteDurableStore, SubmissionPlacement
from codey.durable.checkpoint import operation_from_checkpoint
from codey.durable.errors import CommitConflict
from codey.operations.kernel_driver import drive_provider_request, load_prepared_request, prepare_provider_request
from codey.operations.kernel_errors import RecoveryFailed
from codey.operations.task_effects import KernelEffectSink, KernelRecordedProvider
from codey.operations.task_session import TaskSession
from codey.policies.task_policy import TaskPolicy
from codey.providers.base import ProviderToolDefinition, ProviderToolResult
from codey.runtime.core.operation_reducer import (
    ACTION_COMMIT_GENERATION_BOUNDARY,
    ACTION_INTERPRET_PROVIDER_RESPONSE,
    ACTION_SETTLE_PROVIDER_UNKNOWN,
    ACTION_SUBMIT_PROVIDER_REQUEST,
)
from codey.runtime.core.operation_state import RuntimeOperationTransitionError
from codey.runtime.core.protocol_window import ProtocolWindowRef, ProviderBinding
from codey.runtime.effects.effect_records import RuntimeEffectError, RuntimeEffectIntent
from codey.runtime.write.drive import peek_next_action
from codey.runtime.write.mutation_line import RuntimeMutationLine
from codey.storage.managed_outputs import ManagedOutputStore
from tests.durable.test_provider_response_receipts import runtime


@pytest.mark.parametrize("boundary", ["initial_frozen", "followup_frozen", "previous_settled", "intent_admitted", "response_saved"])
def test_process_exit_preserves_the_original_request_and_dispatch_right(tmp_path, boundary):
    script = r'''
import os, sys
from pathlib import Path
from tests.durable.test_provider_response_receipts import runtime
from codey.operations.kernel_protocol import build_turn_snapshot
from codey.operations.task_session import TaskSession
from codey.operations.task_effects import KernelRecordedProvider
from codey.policies.task_policy import TaskPolicy
root, boundary = Path(sys.argv[1]), sys.argv[2]
durable, line, outputs, sink = runtime(root, sqlite=True)
session = TaskSession(TaskPolicy(frozenset({'control'})), max_turns=4)
sink.bind_request(build_turn_snapshot(session, native=False), logical_turn=1)
class Provider:
    def send(self, prompt):
        with (root / 'calls').open('a', encoding='utf-8') as stream:
            stream.write(prompt + '\n')
        return 'original response'
recorded = KernelRecordedProvider(Provider(), sink)
if boundary in {'followup_frozen', 'previous_settled'}:
    recorded.send('first generation')
    sink.bind_request(build_turn_snapshot(session, native=False), logical_turn=2)
def committed(result, mutation):
    state = mutation.task.state.get('operation') if mutation.task else None
    prepared = state and state.get('leaf') == 'provider_request_ready'
    at_freeze = boundary in {'initial_frozen', 'followup_frozen'} and prepared
    at_boundary = boundary == 'previous_settled' and prepared and bool(mutation.settlements)
    at_intent = boundary == 'intent_admitted' and bool(mutation.effects)
    at_response = boundary == 'response_saved' and any(a.metadata.get('kind') == 'provider_response' for a in mutation.artifacts)
    if at_freeze or at_boundary or at_intent or at_response:
        os._exit(73)
durable.subscribe_commits(committed)
recorded.send('frozen original request')
raise AssertionError('fault point did not exit')
'''
    child = subprocess.run([sys.executable, "-c", script, str(tmp_path), boundary],
                           capture_output=True, text=True, timeout=30)
    assert child.returncode == 73, child.stderr
    durable = DurableSession(SQLiteDurableStore(tmp_path / "runtime.sqlite"))
    durable.recover_scheduler_state()
    durable.claim_task("r")
    line = RuntimeMutationLine(durable=durable)
    line.mark_writer_running("s", "r", provider_id="local")
    sink = KernelEffectSink(line, session_id="s", run_id="r", provider_id="local", durable=durable,
        managed_outputs=ManagedOutputStore(tmp_path))
    provider = Mock(send=Mock(return_value="original response"), window_ref=None)
    recorded = KernelRecordedProvider(provider, sink)
    session = TaskSession(TaskPolicy(frozenset({"control"})), max_turns=4)
    try:
        action = peek_next_action(durable, session_id="s", run_id="r")
        if boundary == "intent_admitted":
            assert action.kind == ACTION_SETTLE_PROVIDER_UNKNOWN
            with pytest.raises(RecoveryFailed, match="settle_provider_unknown"):
                recorded.resume_kernel_response(session)
            provider.send.assert_not_called()
            assert len(durable.effects("r")) == 1
            return
        if boundary != "response_saved":
            assert action.kind == (ACTION_COMMIT_GENERATION_BOUNDARY if boundary == "followup_frozen"
                                   else ACTION_SUBMIT_PROVIDER_REQUEST)
            payload, intent = load_prepared_request(sink, action.request_ref)
            assert payload["args"] == ["frozen original request"]
            assert durable.artifact(action.request_ref).owner_effect_id is None
            assert not any(effect.effect_id == intent.effect_id for effect in durable.effects("r"))
        else:
            assert action.kind == ACTION_INTERPRET_PROVIDER_RESPONSE
        for _ in range(2):
            received = recorded.resume_kernel_response(session)
            assert received.reply == "original response"
            assert received.prompt == "frozen original request"
            assert received.logical_turn == (2 if boundary in {"followup_frozen", "previous_settled"} else 1)
        assert provider.send.call_count == (0 if boundary == "response_saved" else 1)
        if provider.send.call_count:
            provider.send.assert_called_once_with("frozen original request")
            effect = durable.effect(intent.effect_id)
            assert effect.intent["request_ref"] == action.request_ref
        assert len(durable.effects("r")) == (2 if boundary in {"followup_frozen", "previous_settled"} else 1)
        assert [entry.kind for entry in durable.entries("s")] == ["user_message"]
    finally:
        durable.close()


@pytest.mark.parametrize("method", ["send_turn", "send_tool_results", "acknowledge_tool_results",
                                    "send_tool_results_bound", "acknowledge_tool_results_bound"])
def test_driver_restores_exact_declarations_results_and_window(tmp_path, method):
    durable, _, _, sink = runtime(tmp_path)
    tools = [ProviderToolDefinition("old_tool", "original", {"type": "object"})]
    results = [ProviderToolResult("original-call", "original result", "original input")]
    window = ProtocolWindowRef.new("s", ProviderBinding("local", "1", "model", "native", "history"))
    expected_tools = list(tools)
    args = ("prompt", tools) if method == "send_turn" else ((results, window, tools) if method.endswith("_bound") else (results, tools))
    provider = SimpleNamespace(**{method: Mock(return_value="received")})
    try:
        ref = prepare_provider_request(sink, method, args, {}, counters=None)
        tools.append(ProviderToolDefinition("new_tool", "must not appear", {}))
        results.clear()
        assert drive_provider_request(sink, provider, ref) == "received"
        actual = getattr(provider, method).call_args.args
        assert actual[-1] == expected_tools
        if method != "send_turn":
            assert actual[0] == [ProviderToolResult("original-call", "original result", "original input")]
        if method.endswith("_bound"):
            assert actual[1] == window
    finally:
        durable.close()


@pytest.mark.parametrize("lost_at", ["freeze", "previous_boundary", "admission"])
def test_lost_commit_acknowledgement_does_not_allocate_or_execute_again(tmp_path, monkeypatch, lost_at):
    durable, _, _, sink = runtime(tmp_path)
    provider = Mock(send=Mock(return_value="received"))
    recorded = KernelRecordedProvider(provider, sink)
    if lost_at == "previous_boundary":
        recorded.send("first")
    commit = durable.store.commit
    lost = False

    def interrupt(mutation):
        nonlocal lost
        result = commit(mutation)
        selected = ((lost_at == "freeze" and any(a.metadata.get("kind") == "provider_request" for a in mutation.artifacts))
            or (lost_at == "previous_boundary" and bool(mutation.settlements))
            or (lost_at == "admission" and bool(mutation.effects)))
        if not lost and selected:
            lost = True
            raise CommitConflict("commit succeeded before acknowledgement was lost")
        return result

    monkeypatch.setattr(durable.store, "commit", interrupt)
    try:
        if lost_at == "admission":
            with pytest.raises(RecoveryFailed, match="already admitted"):
                recorded.send("original")
            assert provider.send.call_count == 0
            assert peek_next_action(durable, session_id="s", run_id="r").kind == ACTION_SETTLE_PROVIDER_UNKNOWN
        else:
            assert recorded.send("original") == "received"
            assert provider.send.call_count == (2 if lost_at == "previous_boundary" else 1)
        assert lost
        assert len(durable.effects("r")) == (2 if lost_at == "previous_boundary" else 1)
        assert len([a for a in durable.artifacts_for_task("r") if a.metadata["kind"] == "provider_request"]) == len(durable.effects("r"))
    finally:
        durable.close()


def test_cancellation_after_freezing_prevents_intent_admission(tmp_path):
    durable, line, _, sink = runtime(tmp_path)
    provider = Mock()
    try:
        ref = prepare_provider_request(sink, "send", ("original",), {}, counters=None)
        durable.cancel_task("r", reason="stopped before admission")
        with pytest.raises(CommitConflict, match="running task"):
            drive_provider_request(sink, provider, ref)
        assert not durable.effects("r")
        provider.send.assert_not_called()
        assert operation_from_checkpoint(durable.task("r"), "s").request_ref == ref
        with pytest.raises(RuntimeOperationTransitionError, match="pending operation position"):
            line.mark_terminal("s", "r", stop_reason="stopped", summary_chars=0, turns=0, max_turns=4, provider="local")
    finally:
        durable.close()


def test_internal_previous_generation_boundary_does_not_place_user_input(tmp_path):
    durable, _, _, sink = runtime(tmp_path)
    provider = KernelRecordedProvider(Mock(send=Mock(return_value="received")), sink)
    try:
        provider.send("first")
        queued = durable.admit_input(conversation_id="s", request_id="steer", task_id="steer",
            text="new user input", provider_snapshot={}, placement=SubmissionPlacement.QUEUED, mode="steer")
        provider.send("frozen followup")
        assert durable.store.submission(queued.submission_id).placement == SubmissionPlacement.QUEUED
        assert [entry.payload["text"] for entry in durable.entries("s") if entry.kind == "user_message"] == ["inspect"]
        assert "generation_boundary" not in durable.task("r").state
    finally:
        durable.close()


def test_schema_three_migration_preserves_budgets_and_position(tmp_path):
    durable, _, _, sink = runtime(tmp_path)
    try:
        KernelRecordedProvider(Mock(send=Mock(return_value="original")), sink).send("original")
        task = durable.task("r")
        original = operation_from_checkpoint(task, "s")
        payload = {**original.to_payload(), "schema_version": 3}
        migrated = operation_from_checkpoint(replace(task, state={**task.state, "operation": payload}), "s")
        assert migrated == original
        assert operation_from_checkpoint(replace(task, state={**task.state, "operation": migrated.to_payload()}), "s") == original
    finally:
        durable.close()


def test_corrupt_prepared_request_blocks_before_effect_or_provider_execution(tmp_path):
    from pathlib import Path

    durable, _, _, sink = runtime(tmp_path)
    provider = Mock()
    try:
        ref = prepare_provider_request(sink, "send", ("original",), {}, counters=None)
        Path(durable.artifact(ref).storage_path).write_text("corrupt", encoding="utf-8")
        with pytest.raises(ValueError, match="digest"):
            drive_provider_request(sink, provider, ref)
        assert not durable.effects("r")
        provider.send.assert_not_called()
    finally:
        durable.close()


def test_original_effect_codec_retains_its_version_and_new_reference_has_one_owner():
    intent = RuntimeEffectIntent("original", "provider_send", "s", "r", lane="task:r",
                                 operation_id="task:r", schema_version=1)
    assert RuntimeEffectIntent.from_payload(intent.to_payload()) == intent
    current = replace(intent, schema_version=2, request_ref="artifact_original")
    assert RuntimeEffectIntent.from_payload(current.to_payload()) == current
    with pytest.raises(RuntimeEffectError, match="version-2 provider intent"):
        replace(current, schema_version=1)
    with pytest.raises(RuntimeEffectError, match="version-2 provider intent"):
        replace(current, effect_category="tool_call")


def test_version_one_request_artifact_is_decoded_without_a_new_request(tmp_path):
    from codey.operations.kernel_protocol import build_turn_snapshot
    from codey.operations.provider_receipts import snapshot_description

    durable, line, outputs, sink = runtime(tmp_path)
    session = TaskSession(TaskPolicy(frozenset({"control"})), max_turns=4)
    provider = Mock(window_ref=None)
    try:
        payload = {"method": "send", "args": ["original"], "kwargs": {}, "logical_turn": 1,
                   "window": None, "snapshot": snapshot_description(build_turn_snapshot(session, native=False))}
        ref = outputs.write_json_artifact(session_id="s", run_id="r", payload={
            "schema_version": 1, "kind": "provider_request", "effect_id": "legacy", "payload": payload})
        artifact = outputs.json_artifact_record(ref, owner_task_id="r", owner_effect_id="legacy",
                                               metadata={"kind": "provider_request"})
        line.begin_provider_effect("s", "r", RuntimeEffectIntent("legacy", "provider_send", "s", "r",
            provider_id="local", turn=1, args_digest="legacy-digest", schema_version=1), artifacts=(artifact,))
        sink.record_response("legacy", "saved response")
        for _ in range(2):
            restored = KernelRecordedProvider(provider, sink).resume_kernel_response(session)
            assert restored.reply == "saved response" and restored.prompt == "original"
        provider.send.assert_not_called()
        assert len(durable.effects("r")) == 1
        assert durable.effect("legacy").intent["schema_version"] == 1
    finally:
        durable.close()
