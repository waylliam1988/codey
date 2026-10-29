"""Received responses survive a hard exit independently of business boundaries."""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from unittest.mock import Mock

import pytest

from codey.durable import DurableSession, MemoryDurableStore, SQLiteDurableStore
from codey.durable.checkpoint import operation_from_checkpoint
from codey.durable.errors import CommitConflict
from codey.operations.provider_receipts import RESPONSE_KIND, read_provider_artifact
from codey.operations.task_effects import KernelEffectSink
from codey.providers.base import AssistantTurn, ProviderToolCall, TurnFinish
from codey.providers.response_record import decode_reply, encode_reply
from codey.runtime.core.operation_reducer import ACTION_INTERPRET_PROVIDER_RESPONSE, ACTION_SETTLE_PROVIDER_UNKNOWN
from codey.runtime.core.operation_state import RuntimeOperationTransitionError
from codey.runtime.write.drive import peek_next_action
from codey.runtime.write.mutation_line import RuntimeMutationLine
from codey.storage.managed_outputs import ManagedOutputStore
from tests.support.provider_actions import ProviderActions


def runtime(tmp_path, *, sqlite=False, task_kind="chat"):
    durable = DurableSession(SQLiteDurableStore(tmp_path / "runtime.sqlite") if sqlite else MemoryDurableStore())
    durable.admit_input(conversation_id="s", request_id="input", task_id="r", text="inspect", provider_snapshot={})
    durable.claim_task("r")
    durable.start_task("r", state=dict(durable.task("r").state))
    mutations = RuntimeMutationLine(durable=durable)
    mutations.accept_operation(session_id="s", run_id="r", provider_id="local", turn_budget=4, max_repair_rounds=1,
                               task_kind=task_kind)
    mutations.mark_writer_running("s", "r", provider_id="local")
    outputs = ManagedOutputStore(tmp_path)
    sink = KernelEffectSink(mutations, session_id="s", run_id="r", provider_id="local", durable=durable,
                            managed_outputs=outputs)
    return durable, mutations, outputs, sink


def test_native_response_codec_keeps_raw_bad_json_and_finish_information():
    reply = AssistantTurn(text="partial", tool_calls=(ProviderToolCall("", "read_file", {}),),
        raw={"tool_calls": [{"function": {"arguments": '{"path":'}}]},
        finish=TurnFinish.PROTOCOL_ERROR, reasoning="reason")
    assert decode_reply(encode_reply(reply)) == reply
    assert decode_reply(encode_reply('{"tool": broken')) == '{"tool": broken'


def test_receiving_response_does_not_place_history_or_settle_generation(tmp_path):
    durable, mutations, outputs, sink = runtime(tmp_path)
    provider = Mock()
    provider.send.return_value = '{"tool":"read_file","args":{"path":"a.py"}}'
    before = durable.entries("s")
    try:
        original = ProviderActions(provider, sink).send("inspect")
        effect = durable.effects("r")[0]
        assert effect.settlement is None
        assert durable.entries("s") == before
        action = peek_next_action(durable, session_id="s", run_id="r")
        assert action.kind == ACTION_INTERPRET_PROVIDER_RESPONSE
        artifact = durable.artifact(action.response_ref)
        assert decode_reply(read_provider_artifact(outputs, artifact, kind=RESPONSE_KIND,
            session_id="s", run_id="r", effect_id=effect.effect_id)) == original
        sink = KernelEffectSink(mutations, session_id="s", run_id="r", provider_id="local", durable=durable,
                                managed_outputs=outputs)
        pending = sink.pending_provider_settlement()
        assert pending is not None and pending[0].effect_id == effect.effect_id
        assert sink.pending_provider_settlement() == pending
        assert not hasattr(sink, "_pending_provider_settlement")
        mutations.settle_provider_effect_at_boundary("s", "r", pending[0], boundary="settle", messages=pending[1])
        assert sink.pending_provider_settlement() is None
        assert provider.send.call_count == 1
    finally:
        durable.close()


@pytest.mark.parametrize("stop", ["failure", "cancellation"])
def test_scheduler_stop_preserves_received_response_and_its_open_generation(tmp_path, stop):
    durable, mutations, outputs, sink = runtime(tmp_path)
    provider = Mock(send=Mock(return_value="received before controller stopped"))
    try:
        reply = ProviderActions(provider, sink).send("inspect")
        before = operation_from_checkpoint(durable.task("r"), "s")
        effects = durable.effects("r")
        if stop == "failure":
            durable.fail_task("r", "controller failure", state={"stop_reason": "error"})
        else:
            durable.cancel_task("r", reason="user stopped")
        assert operation_from_checkpoint(durable.task("r"), "s") == before
        assert durable.effects("r") == effects
        assert durable.task("r").pending_effect_id == effects[0].effect_id
        assert decode_reply(read_provider_artifact(outputs, durable.artifact(before.response_ref),
            kind=RESPONSE_KIND, session_id="s", run_id="r", effect_id=effects[0].effect_id)) == reply
        with pytest.raises(RuntimeOperationTransitionError, match="pending effects"):
            mutations.mark_terminal("s", "r", stop_reason="error", summary_chars=0,
                                     turns=0, max_turns=4, provider="local")
        assert provider.send.call_count == 1
    finally:
        durable.close()


def test_result_commit_conflict_retries_persistence_without_resending(tmp_path, monkeypatch):
    durable, _, _, sink = runtime(tmp_path)
    commit = durable.store.commit
    failed = []

    def conflict(mutation):
        if any(artifact.metadata.get("kind") == RESPONSE_KIND for artifact in mutation.artifacts) and not failed:
            failed.append(True)
            raise CommitConflict("injected response commit conflict")
        return commit(mutation)

    monkeypatch.setattr(durable.store, "commit", conflict)
    provider = Mock(send=Mock(return_value="received"))
    try:
        assert ProviderActions(provider, sink).send("inspect") == "received"
        assert failed and provider.send.call_count == 1
        assert sink.pending_provider_settlement() is not None
    finally:
        durable.close()


def test_committed_response_retry_does_not_repeat_provider_or_native_history(tmp_path, monkeypatch):
    durable, _, _, sink = runtime(tmp_path)
    reply = AssistantTurn(tool_calls=(ProviderToolCall("original", "read_file", {"path": "a.py"}),))
    provider = Mock(send=Mock(return_value=reply))
    commit = durable.store.commit
    interrupted = False

    def lost_result(mutation):
        nonlocal interrupted
        result = commit(mutation)
        if not interrupted and any(a.metadata.get("kind") == RESPONSE_KIND for a in mutation.artifacts):
            interrupted = True
            raise CommitConflict("response committed before the caller saw success")
        return result

    monkeypatch.setattr(durable.store, "commit", lost_result)
    try:
        assert ProviderActions(provider, sink).send("inspect") == reply
        assert interrupted and provider.send.call_count == 1
        assert len(durable.effects("r")) == 1
        assert [entry.kind for entry in durable.entries("s")] == ["user_message", "tool_call"]
        assert sink.pending_provider_settlement() is not None
    finally:
        durable.close()


def test_recovery_of_admitted_provider_without_response_records_unknown_without_resending(tmp_path):
    from types import SimpleNamespace

    from codey.runtime.effects.effect_records import RuntimeEffectIntent
    from tests.support.receipt_fixtures import inspect_receipt_facts

    durable, line, _, sink = runtime(tmp_path)
    line.begin_provider_effect("s", "r", RuntimeEffectIntent(
        "original", "provider_send", "s", "r", provider_id="local", turn=1, args_digest="request-digest",
    ))
    try:
        result = inspect_receipt_facts(
            SimpleNamespace(state=SimpleNamespace(durable=durable), runtime_mutations=line,
                            runtime_effects=None, tool_result_delivery=None),
            session_id="s", run_id="r", project="", task_kind="chat",
        )
        assert result.ok
        assert durable.effect("original").settlement is None
        from codey.operations.task_loop import run_task_kernel
        from codey.operations.task_session import TaskSession
        from codey.policies.task_policy import TaskPolicy
        from tests.durable.test_operation_control_actions import request_for

        failure = run_task_kernel(TaskSession(TaskPolicy(frozenset({"control"}))),
            request=request_for(durable, sink, Mock()))
        assert failure.stop_reason == "recovery_failure"
        assert len(durable.effects("r")) == 1
        assert durable.effect("original").settlement["sent_state"] == "maybe_sent"
        assert durable.task("r").phase == "running"
    finally:
        durable.close()


def test_corrupt_received_body_blocks_without_reexecuting_provider(tmp_path):
    durable, _, _, sink = runtime(tmp_path)
    provider = Mock(send=Mock(return_value="received"))
    try:
        ProviderActions(provider, sink).send("inspect")
        artifact = next(a for a in durable.artifacts_for_task("r") if a.metadata["kind"] == RESPONSE_KIND)
        Path(artifact.storage_path).write_text("corrupt", encoding="utf-8")
        with pytest.raises(ValueError, match="digest"):
            sink.pending_provider_settlement()
        assert provider.send.call_count == 1
    finally:
        durable.close()


@pytest.mark.parametrize("received", [False, True])
def test_hard_process_exit_distinguishes_received_response_from_unknown_submission(tmp_path, received):
    script = r'''
import os, sys
from pathlib import Path
from tests.durable.test_provider_response_receipts import runtime
from tests.support.provider_actions import ProviderActions
root = Path(sys.argv[1])
received = sys.argv[2] == 'True'
durable, mutations, outputs, sink = runtime(root, sqlite=True)
class Provider:
    def send(self, prompt):
        (root / 'sent').write_text('once', encoding='utf-8')
        if not received:
            os._exit(73)
        return '{"tool":"read_file","args":{"path":"a.py"}}'
def committed(result, mutation):
    if any(a.metadata.get('kind') == 'provider_response' for a in mutation.artifacts):
        os._exit(73)
durable.subscribe_commits(committed)
ProviderActions(Provider(), sink).send('inspect')
raise AssertionError('fault did not exit')
'''
    child = subprocess.run([sys.executable, "-c", script, str(tmp_path), str(received)],
                           capture_output=True, text=True, timeout=30)
    assert child.returncode == 73, child.stderr
    durable = DurableSession(SQLiteDurableStore(tmp_path / "runtime.sqlite"))
    try:
        action = peek_next_action(durable, session_id="s", run_id="r")
        assert action.kind == (ACTION_INTERPRET_PROVIDER_RESPONSE if received else ACTION_SETTLE_PROVIDER_UNKNOWN)
        assert (tmp_path / "sent").read_text(encoding="utf-8") == "once"
        assert len(durable.effects("r")) == 1
        sink = KernelEffectSink(RuntimeMutationLine(durable=durable), session_id="s", run_id="r", provider_id="local",
            durable=durable, managed_outputs=ManagedOutputStore(tmp_path))
        assert (sink.pending_provider_settlement() is not None) == received
        assert durable.entries("s")[-1].kind == "user_message"
        assert (operation_from_checkpoint(durable.task("r"), "s").response_ref != "") == received
    finally:
        durable.close()


@pytest.mark.parametrize("native", [False, True])
def test_restart_executes_saved_response_before_any_new_provider_request(tmp_path, native):
    script = r'''
import os, sys
from pathlib import Path
from tests.durable.test_provider_response_receipts import runtime
from tests.support.provider_actions import ProviderActions
from codey.operations.task_session import TaskSession
from codey.operations.kernel_protocol import build_turn_snapshot
from codey.policies.task_policy import TaskPolicy
from codey.providers.base import AssistantTurn, ProviderToolCall
durable, mutations, outputs, sink = runtime(Path(sys.argv[1]), sqlite=True)
native = sys.argv[2] == 'True'
session = TaskSession(TaskPolicy(frozenset({'control','project.read'})), max_turns=4)
sink.bind_request(build_turn_snapshot(session, native=native), logical_turn=2)
class Provider:
    def send(self, prompt):
        return AssistantTurn(tool_calls=(ProviderToolCall('original-call', 'read_file', {'path':'a.py'}),)) if native else '{"tool":"read_file","args":{"path":"a.py"}}'
def committed(result, mutation):
    if any(a.metadata.get('kind') == 'provider_response' for a in mutation.artifacts):
        os._exit(73)
durable.subscribe_commits(committed)
ProviderActions(Provider(), sink).send('inspect')
'''
    child = subprocess.run([sys.executable, "-c", script, str(tmp_path), str(native)], capture_output=True,
                           text=True, timeout=30)
    assert child.returncode == 73, child.stderr
    from codey.operations.generation_boundary import durable_generation_boundary_callback
    from codey.operations.task_session import TaskSession
    from codey.policies.task_policy import TaskPolicy
    from codey.runtime.core.models import ToolResult
    from tests.support.kernel_runtime import (
        KernelExecutionDeps,
        KernelObservationDeps,
        KernelRunRequest,
        KernelTransportDeps,
        run_task_kernel,
    )

    durable = DurableSession(SQLiteDurableStore(tmp_path / "runtime.sqlite"))
    durable.recover_scheduler_state()
    durable.claim_task("r")
    line = RuntimeMutationLine(durable=durable)
    line.mark_writer_running("s", "r", provider_id="local")
    sink = KernelEffectSink(line, session_id="s", run_id="r", provider_id="local", durable=durable,
        managed_outputs=ManagedOutputStore(tmp_path))
    order = []

    class Provider:
        native_tools = native

        def send(self, prompt):
            order.append("request")
            return '{"tool":"done","args":{"summary":"finished"}}'

        def send_turn(self, prompt, tools):
            raise AssertionError("the original provider request must not be repeated")

        def send_tool_results(self, results, tools):
            assert [result.call_id for result in results] == ["original-call"]
            order.append("request")
            return AssistantTurn(tool_calls=(ProviderToolCall("done-call", "done", {"summary": "finished"}),))

        def acknowledge_tool_results(self, results, tools):
            order.append("acknowledge")
            return AssistantTurn()

    def read(call):
        order.append("execute")
        return ToolResult(call, "saved read", ok=True)

    try:
        result = run_task_kernel(TaskSession(TaskPolicy(frozenset({"control", "project.read"})), max_turns=4),
            request=KernelRunRequest(transport=KernelTransportDeps(provider=Provider(),
                provider_id="local", run_id="r"), execution=KernelExecutionDeps(executors={"read_file": read}),
                observation=KernelObservationDeps(intent_sink=sink, on_boundary=durable_generation_boundary_callback(
                    durable=durable, session_id="s", task_id="r", sink=sink))))
        assert result.completed and result.turns == 3, result
        assert order == (["execute", "request", "acknowledge"] if native else ["execute", "request"])
        assert len([effect for effect in durable.effects("r") if effect.category == "tool_call"]) == 1
        assert all(effect.settlement is not None for effect in durable.effects("r"))
    finally:
        durable.close()


@pytest.mark.parametrize("sqlite", [False, True])
def test_history_projects_the_single_response_body_and_corruption_never_replays(tmp_path, sqlite):
    from codey.operations.generation_boundary import durable_generation_boundary_callback
    from codey.operations.kernel_protocol import build_turn_snapshot
    from codey.operations.task_loop import KernelObservationDeps, KernelRunRequest, KernelTransportDeps, run_task_kernel
    from codey.operations.task_session import TaskSession
    from codey.policies.task_policy import TaskPolicy

    durable, _, _, sink = runtime(tmp_path, sqlite=sqlite)
    session = TaskSession(TaskPolicy(frozenset({"project.read", "control"})), max_turns=4)
    sink.bind_request(build_turn_snapshot(session, native=True), logical_turn=1)
    reply = AssistantTurn(text="original body", tool_calls=(ProviderToolCall("one", "read_file", {"path": "a.py"}),))
    provider = Mock(send=Mock(return_value=reply))
    try:
        ProviderActions(provider, sink).send("inspect")
        state = operation_from_checkpoint(durable.task("r"), "s")
        stored = [entry for entry in durable.store.entries("s") if entry.kind == "tool_call"]
        assert len(stored) == 1 and dict(stored[0].payload) == {"response_ref": state.response_ref}
        projected = [entry for entry in durable.entries("s") if entry.kind == "tool_call"][0]
        assert projected.payload["content"] == reply.text
        assert projected.payload["tool_calls"][0]["id"] == "one"
        assert durable.context("s")[-1].payload["tool_calls"][0]["id"] == "one"
        artifact = durable.artifact(state.response_ref)
        Path(artifact.storage_path).write_text("corrupted", encoding="utf-8")
        with pytest.raises(ValueError, match="digest"):
            durable.history_page("s")
        provider.reset_mock()
        result = run_task_kernel(session, request=KernelRunRequest(
            transport=KernelTransportDeps(provider=provider, provider_id="local", run_id="r"),
            observation=KernelObservationDeps(intent_sink=sink, on_boundary=durable_generation_boundary_callback(
                durable=durable, session_id="s", task_id="r", sink=sink))))
        assert not result.completed and "digest" in result.summary
        assert provider.send.call_count == provider.send_turn.call_count == provider.send_tool_results.call_count == 0
        assert len(durable.effects("r")) == 1
    finally:
        durable.close()
