"""Scheduler termination preserves effects and atomically updates submissions."""

import subprocess
import sys
from pathlib import Path

import pytest

from codey.durable import DurableSession, EffectRecord, MemoryDurableStore, SQLiteDurableStore, SubmissionStatus


def running_tools(session: DurableSession) -> None:
    session.admit_input(conversation_id="conversation", request_id="request", task_id="task",
                        text="inspect", provider_snapshot={"provider_id": "local"})
    session.start_task("task", state={})
    session.record_effect_intents("task", tuple(
        EffectRecord(f"effect-{index}", "task", "tool_call", "unsafe", f"digest-{index}", "pending", {
            "lane": "writer", "operation_id": "operation", "session_id": "conversation", "run_id": "task",
        }) for index in range(2)
    ))


@pytest.mark.parametrize("backend", ["memory", "sqlite"])
def test_failure_preserves_effects_and_commits_checkpoint_submission_once(backend: str, tmp_path: Path) -> None:
    store = MemoryDurableStore() if backend == "memory" else SQLiteDurableStore(tmp_path / "durable.sqlite3")
    session = DurableSession(store)
    running_tools(session)
    before = session.commit_sequence()
    effects = session.effects("task")

    result = session.fail_task("task", "worker stopped", state={"reason": "cancelled externally"})

    assert result.sequence == before + 1
    task = session.task("task")
    assert task.phase == "failed" and task.pending_effect_id == "effect-1"
    assert task.state["pending_effect_ids"] == ("effect-0", "effect-1")
    assert store.submission(task.submission_id).status is SubmissionStatus.UNANSWERED
    assert session.effects("task") == effects
    [record] = session.commits_after(before)
    projections = record.to_payload()["projections"]
    assert sum(event["type"] == "effect_settled" for event in projections) == 0
    assert sum(event["type"] == "submission_completed" for event in projections) == 1


def test_failure_during_submission_update_rolls_back_task_stop(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import codey.durable.sqlite as sqlite_store

    path = tmp_path / "durable.sqlite3"
    session = DurableSession(SQLiteDurableStore(path))
    running_tools(session)
    before = session.commit_sequence()
    original_write = sqlite_store._write_submission_update

    def crash_on_submission(database, update, entries):
        original_write(database, update, entries)
        if update is not None:
            raise OSError("crash during submission update")

    monkeypatch.setattr(sqlite_store, "_write_submission_update", crash_on_submission)
    with pytest.raises(OSError, match="submission update"):
        session.fail_task("task", "worker stopped", state={})

    reopened = DurableSession(SQLiteDurableStore(path))
    assert reopened.commit_sequence() == before
    assert reopened.task("task").phase == "running"
    assert {effect.status for effect in reopened.effects("task")} == {"pending"}
    assert reopened.store.submission(reopened.task("task").submission_id).status is SubmissionStatus.RUNNING
    assert reopened.commits_after(before) == ()


def test_process_death_after_task_write_rolls_back_the_whole_failure(tmp_path: Path) -> None:
    path = tmp_path / "durable.sqlite3"
    session = DurableSession(SQLiteDurableStore(path))
    running_tools(session)
    before = session.commit_sequence()
    session.close()
    script = """
import os, sys
import codey.durable.sqlite as storage
from codey.durable import DurableSession, SQLiteDurableStore
original = storage._write_task
def crash(database, task):
    result = original(database, task)
    if task is not None:
        os._exit(74)
    return result
storage._write_task = crash
DurableSession(SQLiteDurableStore(sys.argv[1])).fail_task('task', 'stopped', state={})
"""
    result = subprocess.run([sys.executable, "-c", script, str(path)], timeout=20)
    assert result.returncode == 74
    reopened = DurableSession(SQLiteDurableStore(path))
    assert reopened.commit_sequence() == before
    assert reopened.task("task").phase == "running"
    assert {effect.status for effect in reopened.effects("task")} == {"pending"}
    assert reopened.store.submission(reopened.task("task").submission_id).status is SubmissionStatus.RUNNING
