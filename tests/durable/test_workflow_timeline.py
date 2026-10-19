from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

from codey.durable import CommitConflict, DurableSession, EffectRecord, MemoryDurableStore, SQLiteDurableStore


@pytest.mark.parametrize("backend", ["memory", "sqlite"])
def test_timeline_reconstructs_checkpoint_and_effects_at_the_same_commit(backend: str, tmp_path: Path) -> None:
    path = tmp_path / "durable.sqlite3"
    store = MemoryDurableStore() if backend == "memory" else SQLiteDurableStore(path)
    durable = DurableSession(store)
    admitted = durable.admit_input(conversation_id="c", request_id="request", task_id="task", text="read",
                                   provider_snapshot={"provider_id": "frozen"})
    durable.start_task("task", state={"step": 1})
    durable.advance_task(task_id="task", phase="assistant_response_pending", state={"step": 2})
    effect = EffectRecord("read-1", "task", "tool_call", "safe_read", "digest", "pending", {"tool": "read"})
    intent = durable.record_effect_intent("task", effect)
    expected = durable.task("task")
    settled = durable.settle_effect("task", replace(effect, status="completed", settlement={"output": "one"}))
    try:
        first = durable.task_at("task", admitted.sequence)
        pending = durable.task_at("task", intent.sequence)
        complete = durable.task_at("task", settled.sequence)
        assert first.task.phase == "accepted"
        assert first.effects == ()
        assert pending.task == expected
        assert pending.effects == (effect,)
        assert complete.task.phase == "tool_outcome_ready"
        assert complete.effects[0].settlement == {"output": "one"}
        assert first.head_entry_id == durable.entries("c")[0].entry_id
        assert [point.commit_seq for point in durable.task_timeline("task")] == sorted(
            point.commit_seq for point in durable.task_timeline("task"))
        # Public event replay must never contain checkpoint/provider/receipt bodies.
        for record in durable.commits_after(0):
            payload = record.to_payload()
            assert "_workflow" not in payload
            assert "frozen" not in str(payload)
            assert "output" not in str(payload)
        if backend == "sqlite":
            durable.close()
            durable = DurableSession(SQLiteDurableStore(path))
            assert durable.task_at("task", intent.sequence) == pending
            assert durable.task_at("task", settled.sequence) == complete
        with pytest.raises(KeyError):
            durable.task_at("task", admitted.sequence - 1)
    finally:
        durable.close()


def test_failed_mutation_never_becomes_a_timeline_point(tmp_path: Path) -> None:
    durable = DurableSession(SQLiteDurableStore(tmp_path / "durable.sqlite3"))
    try:
        durable.admit_input(conversation_id="c", request_id="r", task_id="t", text="go", provider_snapshot={})
        old = durable.task_timeline("t")
        with pytest.raises(CommitConflict):
            durable.update_task_state("t", {"bad": True}, expected_revision=100)
        assert durable.task_timeline("t") == old
    finally:
        durable.close()
