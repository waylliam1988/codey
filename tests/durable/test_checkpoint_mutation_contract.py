"""Checkpoint callbacks retain the same inputs across transaction retries."""

from collections.abc import Callable, Mapping
from pathlib import Path

import pytest

from codey.durable import DurableSession, MemoryDurableStore, SQLiteDurableStore
from codey.durable.errors import CommitConflict
from codey.durable.model import CommitResult, DurableMutation, EffectRecord, TaskCheckpoint
from codey.runtime.effects.effect_records import RuntimeEffectIntent, compute_args_digest
from codey.runtime.effects.replay_policy import ReplayClass
from codey.runtime.effects.tool_result_delivery import DeliveryBatchIntent, DeliveryBatchItem, compute_batch_digest
from codey.runtime.write.mutation_line import RuntimeMutationLine


def _runtime(tmp_path: Path, backend: str) -> tuple[DurableSession, RuntimeMutationLine]:
    store = MemoryDurableStore() if backend == "memory" else SQLiteDurableStore(tmp_path / "durable.sqlite3")
    durable = DurableSession(store)
    durable.admit_input(conversation_id="s", request_id="r", task_id="r", text="read code",
                        provider_snapshot={"provider_id": "local"})
    durable.start_task("r", state={})
    line = RuntimeMutationLine(durable=durable)
    line.accept_operation(session_id="s", run_id="r", provider_id="local", turn_budget=8,
                          max_repair_rounds=1, task_kind="project")
    line.mark_writer_running("s", "r", provider_id="local")
    return durable, line


@pytest.mark.parametrize("backend", ["memory", "sqlite"])
@pytest.mark.parametrize("conflicts", [1, 2, 3])
def test_tool_batch_iterator_survives_checkpoint_conflicts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, backend: str, conflicts: int,
) -> None:
    durable, line = _runtime(tmp_path, backend)
    original_transact = durable.transact_task
    attempts = 0

    def transact(
        task_id: str,
        derive: Callable[[TaskCheckpoint, tuple[EffectRecord, ...], int], DurableMutation | None],
    ) -> CommitResult | None:
        nonlocal attempts
        attempts += 1
        if attempts <= conflicts:
            # Derivation already happened when another writer wins the CAS.
            derive(durable.task(task_id), durable.effects(task_id), durable.head("s")[1])
            raise CommitConflict("injected competing writer")
        return original_transact(task_id, derive)

    monkeypatch.setattr(durable, "transact_task", transact)
    intent = RuntimeEffectIntent(
        "read-1", "tool_call", "s", "r", turn=1, tool_index=0, tool_name="read",
        call_id="call-1", args_digest=compute_args_digest({"path": "a.py"}),
        replay_class=ReplayClass.SAFE, replay_args={"path": "a.py"},
    )
    items = (DeliveryBatchItem(0, "read", "read-1", "safe"),)
    delivery = DeliveryBatchIntent("batch-1", "s", "r", 1, items, compute_batch_digest(items))
    before_task = durable.task("r")
    before_sequence = durable.commit_sequence()

    try:
        if conflicts == 3:
            with pytest.raises(CommitConflict, match="competing writer"):
                line.begin_tool_batch("s", "r", intents=iter((intent,)), delivery_intent=delivery)
            assert durable.task("r") == before_task
            assert durable.effects("r") == ()
            assert durable.commit_sequence() == before_sequence
        else:
            committed = line.begin_tool_batch("s", "r", intents=iter((intent,)), delivery_intent=delivery)
            assert committed.effect_ids == ("read-1",)
            assert durable.effect("read-1").settlement is None
            assert durable.commit_sequence() == before_sequence + 1
            operation = durable.task("r").state["operation"]
            assert isinstance(operation, Mapping)
            assert operation["leaf"] == "tool_effect_pending"
        assert attempts == min(conflicts + 1, 3)
    finally:
        durable.close()
