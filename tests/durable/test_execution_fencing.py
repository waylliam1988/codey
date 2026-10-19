"""Restart and queue ownership are durable facts, not in-memory scheduling sets."""

from dataclasses import replace
from pathlib import Path

import pytest

from codey.durable import CommitConflict, DurableSession, EffectRecord, MemoryDurableStore, SQLiteDurableStore
from codey.durable.model import DurableMutation, TaskStatus
from codey.durable.scheduler import DurableTaskScheduler


@pytest.fixture(params=["memory", "sqlite"])
def sessions(request, tmp_path: Path):
    if request.param == "memory":
        store = MemoryDurableStore()
        first, second = DurableSession(store), DurableSession(store)
    else:
        path = tmp_path / "durable.sqlite3"
        first, second = DurableSession(SQLiteDurableStore(path)), DurableSession(SQLiteDurableStore(path))
    first.admit_input(conversation_id="chat", request_id="request", task_id="task", text="work", provider_snapshot={})
    return first, second


def test_scheduler_dispatches_the_claimed_checkpoint_with_a_new_attempt(sessions):
    session, _ = sessions
    received = []
    scheduler = DurableTaskScheduler(session, lambda task: received.append(task) or True)
    scheduler._reconcile()
    assert len(received) == 1
    checkpoint = received[0]
    assert checkpoint == session.task("task")
    assert checkpoint.status == TaskStatus.RUNNING
    assert checkpoint.attempt_token
    assert checkpoint.state["attempt_number"] == 1


def test_stale_session_cannot_settle_after_restart_even_using_a_fresh_checkpoint(sessions):
    old, restarted = sessions
    old.claim_task("task")
    old.start_task("task", state={})
    effect = EffectRecord("effect", "task", "provider_send", "unsafe", "digest", "pending", {})
    old.record_effect_intent("task", effect)
    old_token = old.task("task").attempt_token
    restarted.recover_scheduler_state()
    restarted.claim_task("task")
    checkpoint = restarted.task("task")
    assert checkpoint.attempt_token != old_token
    assert checkpoint.state["attempt_number"] == 2
    before = restarted.commit_sequence()
    with pytest.raises(CommitConflict, match="attempt"):
        old.settle_effect("task", replace(effect, status="ok", settlement={"receipt": "old worker"}))
    assert restarted.commit_sequence() == before
    assert restarted.effect("effect").settlement is None
    restarted.settle_effect("task", replace(effect, status="interrupted", settlement={"receipt": "unknown"}))
    assert restarted.effect("effect").status == "interrupted"


def test_delayed_worker_keeps_its_attempt_when_same_session_reclaims_task(sessions):
    session, _ = sessions
    session.claim_task("task")
    token = session.task("task").attempt_token
    with session.execution("task", token):
        session.unclaim_task("task")
        session.claim_task("task")
        with pytest.raises(CommitConflict, match="attempt"):
            session.start_task("task", state={})


def test_store_fences_direct_mutations_without_a_matching_attempt(sessions):
    session, _ = sessions
    session.claim_task("task")
    current = session.task("task")
    _, revision = session.head("chat")
    mutation = DurableMutation("chat", revision, task=replace(current, revision=current.revision + 1))
    with pytest.raises(CommitConflict, match="attempt"):
        session.store.commit(mutation)


def test_effect_only_write_cannot_bypass_task_attempt_fencing(sessions):
    session, _ = sessions
    session.claim_task("task")
    _, revision = session.head("chat")
    effect = EffectRecord("effect", "task", "provider_send", "unsafe", "digest", "pending", {})
    with pytest.raises(CommitConflict, match="checkpoint"):
        session.store.commit(DurableMutation("chat", revision, effects=(effect,)))


def test_store_rejects_nonterminal_mutation_after_durable_cancel_barrier(sessions):
    session, _ = sessions
    session.claim_task("task")
    session.start_task("task", state={})
    session.request_cancel("task", reason="stop")
    current = session.task("task")
    _, revision = session.head("chat")
    with pytest.raises(CommitConflict, match="cancellation"):
        session.store.commit(DurableMutation(
            "chat", revision,
            task=replace(current, revision=current.revision + 1, phase="assistant_response_pending"),
            expected_attempt_token=current.attempt_token,
        ))


def test_failed_claim_never_assigns_local_execution_ownership(sessions, monkeypatch):
    session, _ = sessions
    original = session.store.commit
    monkeypatch.setattr(session.store, "commit", lambda _mutation: (_ for _ in ()).throw(OSError("disk failed")))
    with pytest.raises(OSError, match="disk failed"):
        session.claim_task("task")
    monkeypatch.setattr(session.store, "commit", original)
    assert session.task("task").status == TaskStatus.PENDING
