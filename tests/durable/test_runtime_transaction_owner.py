"""The production reducer and effect receipts have one transaction owner."""

from pathlib import Path
from types import SimpleNamespace

import pytest

from codey.durable import DurableSession, SQLiteDurableStore
from codey.operations.recovery import recover_effects_for_resume
from codey.operations.task_effects import KernelEffectSink
from codey.providers.api_provider import ApiProvider
from codey.runtime.core.models import ToolCall, ToolResult
from codey.runtime.effects.effect_records import RuntimeEffectIntent, RuntimeEffectSettlement, compute_args_digest
from codey.runtime.effects.replay_policy import ReplayClass
from codey.runtime.effects.tool_result_delivery import ToolResultDeliveryStore
from codey.runtime.write.mutation_line import RuntimeMutationLine


def _runtime(tmp_path: Path):
    durable = DurableSession(SQLiteDurableStore(tmp_path / "durable.sqlite3"))
    durable.admit_input(conversation_id="s", request_id="request", task_id="r", text="read",
                        provider_snapshot={"provider_id": "local"})
    durable.claim_task("r")
    durable.start_task("r", state=dict(durable.task("r").state))
    line = RuntimeMutationLine(durable=durable)
    line.accept_operation(session_id="s", run_id="r", provider_id="local", turn_budget=8,
                          max_repair_rounds=1, task_kind="project")
    line.mark_writer_running("s", "r", provider_id="local")
    return durable, line


def _intent():
    return RuntimeEffectIntent("p1", "provider_send", "s", "r", provider_id="local",
                               turn=1, args_digest="a" * 64, replay_class=ReplayClass.UNSAFE)


def test_runtime_continues_and_reopens_without_reading_or_writing_jsonl(tmp_path, monkeypatch):
    durable, line = _runtime(tmp_path)
    durable.advance_task(task_id="r", phase="assistant_response_pending", state={})

    line.begin_provider_effect("s", "r", _intent())
    before = durable.commit_sequence()
    line.settle_provider_effect("s", "r", RuntimeEffectSettlement(
        "p1", "provider_send", "s", "r", "ok", sent_state="settled"))
    assert durable.commit_sequence() == before + 1
    assert durable.effect("p1").status == "ok"
    assert durable.task("r").phase == "assistant_response_pending"
    assert durable.task("r").state["operation"]["leaf"] == "writer_running"
    durable.close()
    reopened = DurableSession(SQLiteDurableStore(tmp_path / "durable.sqlite3"))
    assert reopened.task("r").state["operation"]["leaf"] == "writer_running"
    assert reopened.effect("p1").status == "ok"
    assert not (tmp_path / "runtime" / "sessions").exists()
    reopened.close()


def test_runtime_settlement_failure_leaves_effect_and_reducer_pending(tmp_path, monkeypatch):
    durable, line = _runtime(tmp_path)
    line.begin_provider_effect("s", "r", _intent())
    before = durable.task("r")

    def fail(mutation):
        raise OSError("injected SQLite commit failure")

    monkeypatch.setattr(durable.store, "commit", fail)
    with pytest.raises(OSError, match="injected"):
        line.settle_provider_effect("s", "r", RuntimeEffectSettlement(
            "p1", "provider_send", "s", "r", "ok", sent_state="settled"))
    # A failed durable commit seals the session. Recovery must reopen the
    # database before inspecting the unchanged facts.
    durable.close()
    reopened = DurableSession(SQLiteDurableStore(tmp_path / "durable.sqlite3"))
    assert reopened.task("r") == before
    assert reopened.effect("p1").settlement is None
    assert not (tmp_path / "runtime" / "sessions").exists()
    reopened.close()


def test_kernel_tool_settlement_and_delivery_ack_are_single_commits(tmp_path, monkeypatch):
    durable, line = _runtime(tmp_path)
    line.begin_provider_effect("s", "r", _intent())
    line.settle_provider_effect("s", "r", RuntimeEffectSettlement(
        "p1", "provider_send", "s", "r", "ok", sent_state="settled"))
    sink = KernelEffectSink(line, session_id="s", run_id="r", provider_id="local", durable=durable)
    call = ToolCall("read", {"path": "a.txt"}, call_id="call-1")
    before = durable.commit_sequence()
    sink.begin_turn([("tool-1", call, 0)], turn=1)
    assert durable.commit_sequence() == before + 1
    assert durable.task("r").state["operation"]["leaf"] == "tool_effect_pending"
    before = durable.commit_sequence()
    sink.settle("tool-1", True, result=ToolResult(ok=True, call=call, model_text="hello"))
    assert durable.commit_sequence() == before + 1
    assert durable.effect("tool-1").status == "ok"
    assert durable.task("r").state["operation"]["leaf"] == "tool_delivery_pending"
    before = durable.commit_sequence()
    line.begin_provider_effect("s", "r", RuntimeEffectIntent(
        "p2", "provider_send", "s", "r", provider_id="local", turn=2,
        args_digest="b" * 64, replay_class=ReplayClass.UNSAFE), delivery_batch_id=sink.delivery_batch_id)
    assert durable.commit_sequence() == before + 1
    before = durable.commit_sequence()
    line.settle_provider_effect("s", "r", RuntimeEffectSettlement(
        "p2", "provider_send", "s", "r", "ok", sent_state="settled"))
    assert durable.commit_sequence() == before + 1
    assert ToolResultDeliveryStore(durable=durable).load_batches("s", "r")[0].is_delivered
    assert not (tmp_path / "runtime" / "sessions").exists()
    durable.close()


def test_provider_binding_cannot_restore_an_independent_history(tmp_path, monkeypatch):
    from codey.providers.context_ledger import ContextLedger

    def forbidden(*args, **kwargs):
        raise AssertionError("provider journal is not a recovery owner")

    monkeypatch.setattr(ContextLedger, "for_protocol_session", forbidden)
    provider = ApiProvider("http://scripted.invalid/v1", "scripted")
    provider.bind_context("s")
    assert provider.context_ledger.root is None
    assert provider._messages == []
    provider.close()


def test_safe_recovery_uses_checkpoint_batch_and_never_runtime_jsonl(tmp_path, monkeypatch):
    from codey.runtime.effects.effect_records import RuntimeEffectStore
    from codey.runtime.effects.tool_result_delivery import DeliveryBatchIntent, DeliveryBatchItem, compute_batch_digest

    (tmp_path / "a.txt").write_text("recovered read", encoding="utf-8")
    durable, line = _runtime(tmp_path)
    line.begin_provider_effect("s", "r", _intent())
    line.settle_provider_effect("s", "r", RuntimeEffectSettlement(
        "p1", "provider_send", "s", "r", "ok", sent_state="settled"))
    items = (DeliveryBatchItem(0, "read", "read-1", "safe"),)
    line.begin_tool_batch("s", "r", intents=(RuntimeEffectIntent(
        "read-1", "tool_call", "s", "r", turn=1, tool_index=0, tool_name="read", call_id="call-1",
        args_digest="a" * 64, replay_class=ReplayClass.SAFE, replay_args={"path": "a.txt"}),),
        delivery_intent=DeliveryBatchIntent("batch-1", "s", "r", 1, items, compute_batch_digest(items)))

    result = recover_effects_for_resume(SimpleNamespace(
        state=SimpleNamespace(durable=durable), runtime_mutations=line,
        runtime_effects=RuntimeEffectStore(durable=durable),
        tool_result_delivery=ToolResultDeliveryStore(durable=durable), managed_outputs=None,
    ), session_id="s", run_id="r", project=str(tmp_path), task_kind="project")
    assert result.ok
    assert result.recovered_tool_result_batch_id == "batch-1"
    assert durable.effect("read-1").status == "ok"
    assert durable.task("r").state["operation"]["leaf"] == "tool_delivery_pending"
    assert any(entry.kind == "tool_result" for entry in durable.entries("s"))
    durable.close()


def test_settled_recovery_reads_receipt_from_durable_without_runtime_projection(tmp_path):
    from codey.operations.recovery import recover_effects_for_resume
    from codey.runtime.effects.tool_result_delivery import DeliveryBatchIntent, DeliveryBatchItem, compute_batch_digest

    durable, line = _runtime(tmp_path)
    sink = KernelEffectSink(line, session_id="s", run_id="r", provider_id="local", durable=durable)
    durable.advance_task(task_id="r", phase="assistant_response_pending", state={})
    call = ToolCall("read", {"path": "a.txt"}, call_id="call-1")
    batch_items = (DeliveryBatchItem(0, "read", "read-1", "safe"),)
    line.begin_tool_batch(
        "s", "r",
        intents=(RuntimeEffectIntent(
            "read-1", "tool_call", "s", "r", turn=1, tool_index=0,
            tool_name="read", call_id="call-1", args_digest=compute_args_digest({"path": "a.txt"}),
            replay_class=ReplayClass.SAFE, replay_args={"path": "a.txt"},
        ),),
        delivery_intent=DeliveryBatchIntent("batch-1", "s", "r", 1, batch_items, compute_batch_digest(batch_items)),
    )
    sink.settle("read-1", True, result=ToolResult(ok=True, call=call, model_text="durable receipt"))
    result = recover_effects_for_resume(
        SimpleNamespace(state=SimpleNamespace(durable=durable), runtime_mutations=None,
                        runtime_effects=None, tool_result_delivery=None, managed_outputs=None),
        session_id="s", run_id="r", project=str(tmp_path), task_kind="project",
    )
    assert result.ok is True
    assert len(result.settled_tool_outcomes) == 1
    assert result.settled_tool_outcomes[0].outcome.model_text == "durable receipt"
    durable.close()


def test_entry_policy_is_one_commit_and_has_one_stored_owner(tmp_path):
    from codey.operations.recovery import record_entry_policy
    from codey.policies.task_policy import TaskPolicy
    from codey.runtime.core.operation_state import RuntimeOperationStore

    durable, line = _runtime(tmp_path)
    policy = TaskPolicy(frozenset({"control", "project.read"}))
    before = durable.commit_sequence()
    record_entry_policy(line, session_id="s", run_id="r", policy=policy, durable=durable)
    assert durable.commit_sequence() == before + 1
    task = durable.task("r")
    assert "task_policy" in task.state
    assert "task_policy" not in task.state["operation"]
    assert RuntimeOperationStore(durable=durable).load("s", "r").task_policy == policy.to_payload()
    durable.close()
