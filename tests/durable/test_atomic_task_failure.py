"""Task termination settles all unknown effects in the same transaction."""

import subprocess
import sys
from pathlib import Path

import pytest

from codey.durable import DurableSession, EffectRecord, MemoryDurableStore, SQLiteDurableStore, SubmissionStatus


def running_tools(session: DurableSession) -> None:
    session.admit_input(conversation_id="conversation", request_id="request", task_id="task",
                        text="inspect", provider_snapshot={"provider_id": "local"})
    session.start_task("task", state={})
    session.advance_task(task_id="task", phase="assistant_intent_committed", state={})
    session.advance_task(task_id="task", phase="assistant_response_pending", state={})
    session.record_effect_intents("task", tuple(
        EffectRecord(f"effect-{index}", "task", "tool_call", "unsafe", f"digest-{index}", "pending", {
            "lane": "writer", "operation_id": "operation", "session_id": "conversation", "run_id": "task",
        }) for index in range(2)
    ))


@pytest.mark.parametrize("backend", ["memory", "sqlite"])
def test_failure_settles_all_effects_checkpoint_submission_and_events_once(backend: str, tmp_path: Path) -> None:
    store = MemoryDurableStore() if backend == "memory" else SQLiteDurableStore(tmp_path / "durable.sqlite3")
    session = DurableSession(store)
    running_tools(session)
    before = session.commit_sequence()

    result = session.fail_task("task", "worker stopped", state={"reason": "cancelled externally"})

    assert result.sequence == before + 1
    task = session.task("task")
    assert task.phase == "failed" and task.pending_effect_id is None
    assert task.state["pending_effect_ids"] == ()
    assert store.submission(task.submission_id).status is SubmissionStatus.UNANSWERED
    assert {effect.status for effect in session.effects("task")} == {"interrupted"}
    [record] = session.commits_after(before)
    projections = record.to_payload()["projections"]
    assert sum(event["type"] == "effect_settled" for event in projections) == 2
    assert sum(event["type"] == "submission_completed" for event in projections) == 1


def test_failure_during_second_settlement_rolls_back_first_effect_too(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import codey.durable.sqlite as sqlite_store

    path = tmp_path / "durable.sqlite3"
    session = DurableSession(SQLiteDurableStore(path))
    running_tools(session)
    before = session.commit_sequence()
    original_write = sqlite_store._write_effect
    calls = 0

    def crash_on_second(database, effect, expected_status):
        nonlocal calls
        if effect is not None:
            calls += 1
            if calls == 2:
                raise OSError("crash during second effect settlement")
        return original_write(database, effect, expected_status)

    monkeypatch.setattr(sqlite_store, "_write_effect", crash_on_second)
    with pytest.raises(OSError, match="second effect settlement"):
        session.fail_task("task", "worker stopped", state={})

    reopened = DurableSession(SQLiteDurableStore(path))
    assert reopened.commit_sequence() == before
    assert reopened.task("task").phase == "tool_effect_running"
    assert {effect.status for effect in reopened.effects("task")} == {"pending"}
    assert reopened.commits_after(before) == ()


def test_process_death_after_first_effect_write_rolls_back_the_whole_failure(tmp_path: Path) -> None:
    path = tmp_path / "durable.sqlite3"
    session = DurableSession(SQLiteDurableStore(path))
    running_tools(session)
    before = session.commit_sequence()
    session.close()
    script = """
import os, sys
import codey.durable.sqlite as storage
from codey.durable import DurableSession, SQLiteDurableStore
original = storage._write_effect
def crash(database, effect, expected_status):
    result = original(database, effect, expected_status)
    if effect is not None:
        os._exit(74)
    return result
storage._write_effect = crash
DurableSession(SQLiteDurableStore(sys.argv[1])).fail_task('task', 'stopped', state={})
"""
    result = subprocess.run([sys.executable, "-c", script, str(path)], timeout=20)
    assert result.returncode == 74
    reopened = DurableSession(SQLiteDurableStore(path))
    assert reopened.commit_sequence() == before
    assert reopened.task("task").phase == "tool_effect_running"
    assert {effect.status for effect in reopened.effects("task")} == {"pending"}
