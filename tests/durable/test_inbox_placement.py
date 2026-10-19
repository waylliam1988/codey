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


def test_scheduler_uses_one_at_a_time_follow_up_boundary_by_default() -> None:
    session = DurableSession(MemoryDurableStore())
    session.admit_input(
        conversation_id="chat", request_id="active", task_id="active",
        text="current", provider_snapshot={},
    )
    session.start_task("active", state={})
    queued_ids = []
    for index in range(3):
        result = session.admit_input(
            conversation_id="chat", request_id=f"queued-{index}", task_id=f"queued-{index}",
            text=f"next-{index}", provider_snapshot={}, placement=SubmissionPlacement.QUEUED,
            mode="follow_up",
        )
        queued_ids.append(str(result.submission_id))
    session.complete_task("active", "done", state={})

    dispatched: list[str] = []
    scheduler = DurableTaskScheduler(session, lambda task: dispatched.append(task.task_id) or True)
    scheduler._reconcile()
    assert dispatched == ["queued-0"]
    assert [session.store.submission(submission_id).placement for submission_id in queued_ids] == [
        SubmissionPlacement.PLACED,
        SubmissionPlacement.QUEUED,
        SubmissionPlacement.QUEUED,
    ]
    scheduler.close()


def test_scheduler_can_place_all_follow_ups_at_one_boundary() -> None:
    session = DurableSession(MemoryDurableStore())
    session.admit_input(
        conversation_id="chat", request_id="active", task_id="active",
        text="current", provider_snapshot={},
    )
    session.start_task("active", state={})
    queued_ids = []
    for index in range(2):
        result = session.admit_input(
            conversation_id="chat", request_id=f"queued-{index}", task_id=f"queued-{index}",
            text=f"next-{index}", provider_snapshot={}, placement=SubmissionPlacement.QUEUED,
            mode="follow_up",
        )
        queued_ids.append(str(result.submission_id))
    session.complete_task("active", "done", state={})

    dispatched: list[str] = []
    scheduler = DurableTaskScheduler(
        session,
        lambda task: dispatched.append(task.task_id) or True,
        follow_up_mode="all",
    )
    scheduler._reconcile()
    assert dispatched == ["queued-0", "queued-1"]
    assert all(session.store.submission(submission_id).placement is SubmissionPlacement.PLACED for submission_id in queued_ids)
    scheduler.close()


def test_steer_is_selected_before_follow_up_at_the_same_boundary(session: DurableSession) -> None:
    session.admit_input(
        conversation_id="chat", request_id="active", task_id="active",
        text="current", provider_snapshot={},
    )
    session.start_task("active", state={})
    follow_up = session.admit_input(
        conversation_id="chat", request_id="follow-up", task_id="follow-up",
        text="later", provider_snapshot={}, placement=SubmissionPlacement.QUEUED,
        mode="follow_up",
    )
    steer = session.admit_input(
        conversation_id="chat", request_id="steer", task_id="steer",
        text="change direction", provider_snapshot={}, placement=SubmissionPlacement.QUEUED,
        mode="steer",
    )
    session.complete_task("active", "done", state={})

    dispatched: list[str] = []
    scheduler = DurableTaskScheduler(session, lambda task: dispatched.append(task.task_id) or True)
    scheduler._reconcile()
    assert dispatched == ["steer"]
    assert session.store.submission(str(steer.submission_id)).placement is SubmissionPlacement.PLACED
    assert session.store.submission(str(follow_up.submission_id)).placement is SubmissionPlacement.QUEUED
    scheduler.close()
