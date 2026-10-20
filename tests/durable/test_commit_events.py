"""Commit publication survives the storage/publication crash boundary."""

import os
import subprocess
import sys
from pathlib import Path
from queue import Queue
from types import SimpleNamespace

import pytest

from codey.durable import DurableSession, EffectRecord, MemoryDurableStore, SQLiteDurableStore


@pytest.fixture(params=["memory", "sqlite"])
def session(request: pytest.FixtureRequest, tmp_path: Path) -> DurableSession:
    store = MemoryDurableStore() if request.param == "memory" else SQLiteDurableStore(tmp_path / "durable.sqlite3")
    return DurableSession(store)


def admit(session: DurableSession, *, task_id: str = "task"):
    return session.admit_input(
        conversation_id="conversation", request_id="request", task_id=task_id,
        text="inspect", provider_snapshot={"provider_id": "local"},
    )


def test_replay_retains_original_conversation_entry_and_task_identity(session: DurableSession) -> None:
    result = admit(session)
    record = session.commits_after(0)[-1]
    payload = record.to_payload()
    assert payload["commit_seq"] == result.sequence
    assert payload["session_id"] == "conversation"
    assert payload["task_id"] == "task"
    assert payload["entry_ids"] == [session.entries("conversation")[0].entry_id]
    assert {event["type"] for event in payload["projections"]} == {
        "conversation_created", "entry_appended", "submission_admitted", "task_progressed",
    }


def test_append_returns_the_committed_entry_with_its_real_sequence(session: DurableSession) -> None:
    session.create_conversation("conversation")
    entry = session.append_message("conversation", "user_message", {"text": "hello"})
    assert entry.commit_seq > 0
    assert entry == session.entries("conversation")[0]


def test_replayed_submission_does_not_publish_an_uncommitted_task_identity(session: DurableSession) -> None:
    published = []
    session._on_commit = lambda result, _mutation: published.append(result.record.to_payload())
    result = admit(session)
    repeated = admit(session, task_id="task-that-was-never-created")
    assert repeated == result
    assert len(published) == 1
    assert published[0]["task_id"] == "task"


def test_effect_settlement_event_and_checkpoint_have_one_commit(session: DurableSession) -> None:
    admit(session)
    session.start_task("task", state={})
    session.advance_task(task_id="task", phase="assistant_intent_committed", state={})
    session.advance_task(task_id="task", phase="assistant_response_pending", state={})
    session.record_effect_intent("task", EffectRecord("effect", "task", "tool_call", "safe", "digest", "pending", {}))
    before = session.commits_after(0)[-1].sequence
    result = session.settle_effect("task", EffectRecord("effect", "task", "tool_call", "safe", "digest", "ok", {}, {"ok": True}))
    [record] = session.commits_after(before)
    assert record.sequence == result.sequence
    events = record.to_payload()["projections"]
    assert any(event["type"] == "effect_settled" and event["effect_id"] == "effect" for event in events)
    assert any(event["type"] == "task_progressed" and event["phase"] == "tool_outcome_ready" for event in events)


def test_restart_after_commit_before_publication_replays_identical_event(tmp_path: Path) -> None:
    path = tmp_path / "durable.sqlite3"
    observed = []

    def publication_crash(result, _mutation):
        observed.append(result.record.to_payload())
        raise RuntimeError("process stopped before SSE delivery")

    session = DurableSession(SQLiteDurableStore(path), on_commit=publication_crash)
    admit(session)
    session.close()
    reopened = DurableSession(SQLiteDurableStore(path))
    assert [record.to_payload() for record in reopened.commits_after(0)] == observed


def test_sqlite_failure_does_not_leave_an_event_or_advance_checkpoint(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import codey.durable.sqlite as sqlite_store

    path = tmp_path / "durable.sqlite3"
    session = DurableSession(SQLiteDurableStore(path))
    admit(session)
    before = session.commits_after(0)

    def fail_write(*_args):
        raise OSError("checkpoint write failed")

    monkeypatch.setattr(sqlite_store, "_write_task", fail_write)
    with pytest.raises(OSError, match="checkpoint write failed"):
        session.start_task("task", state={})
    reopened = DurableSession(SQLiteDurableStore(path))
    assert reopened.commits_after(0) == before
    assert reopened.task("task").phase == "accepted"


def test_real_process_exit_after_commit_before_publication(tmp_path: Path) -> None:
    path = tmp_path / "durable.sqlite3"
    script = """
import os, sys
from codey.durable import DurableSession, SQLiteDurableStore
def die(*_args):
    os._exit(73)
session = DurableSession(SQLiteDurableStore(sys.argv[1]), on_commit=die)
session.admit_input(conversation_id='conversation', request_id='request', task_id='task',
                    text='committed', provider_snapshot={'provider_id':'local'})
"""
    result = subprocess.run([sys.executable, "-c", script, str(path)], timeout=20, env={**os.environ})
    assert result.returncode == 73
    reopened = DurableSession(SQLiteDurableStore(path))
    record = reopened.commits_after(0)[-1]
    assert record.to_payload()["task_id"] == reopened.task("task").task_id
    assert record.to_payload()["entry_ids"] == [reopened.entries("conversation")[0].entry_id]


def test_sse_replays_every_page_and_suppresses_queued_duplicates(monkeypatch: pytest.MonkeyPatch) -> None:
    from codey.app import server

    durable = DurableSession(MemoryDurableStore())
    durable.create_conversation("conversation")
    for index in range(520):
        durable.append_message("conversation", "user_message", {"text": str(index)})
    queue = Queue()
    queue.replay_cutoff = 7
    queue.put(durable.commits_after(520)[0].to_payload())
    queue.put({"type": "end"})
    closed = []
    state = SimpleNamespace(durable=durable, run_status=lambda: {}, subscribe=lambda: queue,
                            unsubscribe=lambda q: closed.append(q))
    monkeypatch.setattr(server, "get_state", lambda: state)
    written = []

    def write(event, *, event_id=0):
        written.append(event)
        return event["type"] != "end"

    handler = SimpleNamespace(
        path=f"/api/events?after_commit_seq=1&database_id={durable.database_id}", headers={},
        send_response=lambda *_args: None, send_header=lambda *_args: None,
        end_headers=lambda: None, _write_sse_event=write,
    )
    server.Handler._sse(handler)
    replayed = [event for event in written if event["type"] == "durable_commit"]
    assert [event["commit_seq"] for event in replayed] == list(range(2, 522))
    assert all(event["session_id"] == "conversation" and event["entry_ids"] for event in replayed)
    assert closed == [queue]
