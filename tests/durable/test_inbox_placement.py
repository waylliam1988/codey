from __future__ import annotations

from pathlib import Path

import pytest

from codey.app.context import AppContext
from codey.durable import DurableSession, MemoryDurableStore, SQLiteDurableStore, SubmissionPlacement
from codey.durable.scheduler import DurableTaskScheduler


@pytest.fixture(params=["memory", "sqlite"])
def session(request: pytest.FixtureRequest, tmp_path: Path) -> DurableSession:
    store = (
        MemoryDurableStore()
        if request.param == "memory"
        else SQLiteDurableStore(tmp_path / "durable.sqlite3")
    )
    return DurableSession(store)


def test_queued_admission_has_no_history_entry(session: DurableSession) -> None:
    result = session.admit_input(
        conversation_id="chat",
        request_id="queued-request",
        task_id="queued-task",
        text="wait until the current run reaches a boundary",
        provider_snapshot={"provider_id": "local"},
        placement=SubmissionPlacement.QUEUED,
        mode="follow_up",
    )

    assert session.entries("chat") == ()
    submission = session.store.submission(str(result.submission_id))
    assert submission.placement is SubmissionPlacement.QUEUED
    assert submission.input_entry_id is None
    task = session.task("queued-task")
    assert task.state["input_state"] == "queued"
    assert "input_entry_id" not in task.state


def test_placement_atomically_adds_entry_and_updates_submission(session: DurableSession) -> None:
    result = session.admit_input(
        conversation_id="chat",
        request_id="queued-request",
        task_id="queued-task",
        text="place me exactly once",
        provider_snapshot={"provider_id": "local"},
        placement=SubmissionPlacement.QUEUED,
    )

    placed = session.place_submission(str(result.submission_id))

    assert placed.submission_id == result.submission_id
    entries = session.entries("chat")
    assert len(entries) == 1
    assert entries[0].kind == "user_message"
    assert entries[0].payload["text"] == "place me exactly once"
    submission = session.store.submission(str(result.submission_id))
    assert submission.placement is SubmissionPlacement.PLACED
    assert submission.input_entry_id == entries[0].entry_id
    task = session.task("queued-task")
    assert task.state["input_state"] == "placed"
    assert task.state["input_entry_id"] == entries[0].entry_id

    retry = session.place_submission(str(result.submission_id))
    assert retry.sequence == placed.sequence
    assert session.entries("chat") == entries


def test_busy_input_is_admitted_queued_without_history_entry(tmp_path: Path) -> None:
    state = AppContext(tmp_path / "state", start_scheduler=False)
    try:
        state.durable.admit_input(
            conversation_id="chat",
            request_id="active-request",
            task_id="active-task",
            text="current run",
            provider_snapshot={"provider_id": "local"},
        )
        state.durable.start_task("active-task", state={})
        item = state.input_queue.create(
            "queued-input",
            {
                "session_id": "chat",
                "run_id": "queued-task",
                "task": "wait for the current run",
                "provider": "local",
                "intent": "chat",
            },
        )

        assert item["state"] == "queued"
        assert len(state.durable.entries("chat")) == 1
        queued = state.durable.store.submission(state.durable.task("queued-task").submission_id)
        assert queued.placement is SubmissionPlacement.QUEUED
        assert queued.input_entry_id is None
    finally:
        state.close()


def test_scheduler_places_queued_input_only_after_active_task_settles() -> None:
    session = DurableSession(MemoryDurableStore())
    session.admit_input(
        conversation_id="chat", request_id="active", task_id="active",
        text="current", provider_snapshot={},
    )
    session.start_task("active", state={})
    queued = session.admit_input(
        conversation_id="chat", request_id="queued", task_id="queued",
        text="next", provider_snapshot={}, placement=SubmissionPlacement.QUEUED,
    )
    assert session.entries("chat")[0].payload["text"] == "current"
    session.complete_task("active", "done", state={})

    dispatched: list[str] = []
    scheduler = DurableTaskScheduler(
        session,
        lambda task: dispatched.append(task.task_id) or True,
        retry_interval=0.01,
    )
    scheduler.start()
    scheduler.close()

    assert dispatched == ["queued"]
    assert session.store.submission(str(queued.submission_id)).placement is SubmissionPlacement.PLACED
    assert [entry.payload["text"] for entry in session.entries("chat")] == ["current", "done", "next"]
