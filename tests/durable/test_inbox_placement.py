from __future__ import annotations

import threading
from pathlib import Path

import pytest

from codey.app.context import AppContext
from codey.durable import (
    DurableSession,
    MemoryDurableStore,
    SQLiteDurableStore,
    SubmissionPlacement,
    SubmissionStatus,
    TaskStatus,
)
from codey.durable.model import thaw_json
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
    accepted = threading.Event()

    def dispatch(task) -> bool:
        dispatched.append(task.task_id)
        accepted.set()
        return True

    scheduler = DurableTaskScheduler(
        session,
        dispatch,
        retry_interval=0.01,
    )
    try:
        scheduler.start()
        # start() launches an asynchronous dispatcher. Closing immediately
        # can legitimately stop it before it has placed or dispatched input.
        assert accepted.wait(2)
        assert dispatched == ["queued"]
        assert session.store.submission(str(queued.submission_id)).placement is SubmissionPlacement.PLACED
        assert [entry.payload["text"] for entry in session.entries("chat")] == ["current", "done", "next"]
    finally:
        assert scheduler.close()
        session.close()


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


def test_generation_boundary_places_steer_at_post_tools_and_follow_up_at_final(
    session: DurableSession,
) -> None:
    """Queued inputs must be admitted at the provider boundary, not terminalization.

    Pi applies a postTools boundary before starting the next generation.  A
    steering input therefore becomes visible before an older follow-up, while
    the follow-up remains queued until the final boundary.
    """
    session.admit_input(
        conversation_id="chat", request_id="active", task_id="active",
        text="current", provider_snapshot={},
    )
    session.start_task("active", state={})
    follow_up = session.admit_input(
        conversation_id="chat", request_id="follow-up", task_id="follow-up",
        text="continue later", provider_snapshot={}, placement=SubmissionPlacement.QUEUED,
        mode="follow_up",
    )
    steer = session.admit_input(
        conversation_id="chat", request_id="steer", task_id="steer",
        text="change direction now", provider_snapshot={}, placement=SubmissionPlacement.QUEUED,
        mode="steer",
    )

    placed = session.place_boundary_inputs("chat", at="post_tools")

    assert [item.request_id for item in placed] == ["steer"]
    assert session.store.submission(str(steer.submission_id)).placement is SubmissionPlacement.PLACED
    assert session.store.submission(str(follow_up.submission_id)).placement is SubmissionPlacement.QUEUED
    assert [entry.payload["text"] for entry in session.entries("chat")] == ["current", "change direction now"]

    placed_final = session.place_boundary_inputs("chat", at="final")

    assert [item.request_id for item in placed_final] == ["follow-up"]
    assert [entry.payload["text"] for entry in session.entries("chat")] == [
        "current", "change direction now", "continue later",
    ]


def test_boundary_places_selected_user_inputs_in_one_durable_commit(
    session: DurableSession,
) -> None:
    session.admit_input(
        conversation_id="chat", request_id="active", task_id="active",
        text="current", provider_snapshot={},
    )
    session.start_task("active", state={})
    first = session.admit_input(
        conversation_id="chat", request_id="steer-1", task_id="steer-1",
        text="first direction", provider_snapshot={}, placement=SubmissionPlacement.QUEUED,
        mode="steer",
    )
    second = session.admit_input(
        conversation_id="chat", request_id="steer-2", task_id="steer-2",
        text="second direction", provider_snapshot={}, placement=SubmissionPlacement.QUEUED,
        mode="steer",
    )
    before = session.commit_sequence()

    placed = session.place_boundary_inputs("chat", at="post_tools", steering_mode="all")

    assert [item.request_id for item in placed] == ["steer-1", "steer-2"]
    assert session.commit_sequence() == before + 1
    assert session.store.submission(str(first.submission_id)).placement is SubmissionPlacement.PLACED
    assert session.store.submission(str(second.submission_id)).placement is SubmissionPlacement.PLACED
    entries = session.entries("chat")
    assert [entry.by_task_id for entry in entries[-2:]] == ["steer-1", "steer-2"]
    assert session.task("steer-1").state["input_state"] == "placed"
    assert session.task("steer-2").state["input_state"] == "placed"
    assert session.store.task_history("steer-1")[-1].task.revision == 1
    assert session.store.task_history("steer-2")[-1].task.revision == 1


def test_display_only_write_is_done_without_task_and_excluded_from_context(
    session: DurableSession,
) -> None:
    result = session.admit_write(
        conversation_id="chat",
        request_id="notice-1",
        kind="notice",
        data={"text": "saved locally"},
        display_only=True,
    )

    submission = session.store.submission(str(result.submission_id))
    assert submission.status is SubmissionStatus.DONE
    assert submission.input_entry_id is not None
    assert session.tasks() == ()
    assert session.context_snapshot("chat", token_budget=100).messages == ()
    assert session.entries("chat")[0].kind == "notice"

    retry = session.admit_write(
        conversation_id="chat",
        request_id="notice-1",
        kind="notice",
        data={"text": "saved locally"},
        display_only=True,
    )
    assert retry.sequence == result.sequence
    assert len(session.entries("chat")) == 1


def test_queued_display_only_write_places_without_creating_a_task() -> None:
    session = DurableSession(MemoryDurableStore())
    result = session.admit_write(
        conversation_id="chat",
        request_id="notice-queued",
        kind="notice",
        data={"text": "later"},
        display_only=True,
        placement=SubmissionPlacement.QUEUED,
    )
    assert session.entries("chat") == ()
    assert session.store.submission(str(result.submission_id)).status is SubmissionStatus.QUEUED

    scheduler = DurableTaskScheduler(session, lambda _task: True)
    scheduler._reconcile()
    scheduler.close()

    submission = session.store.submission(str(result.submission_id))
    assert submission.status is SubmissionStatus.DONE
    assert len(session.entries("chat")) == 1
    assert session.tasks() == ()


def test_display_only_write_can_land_while_a_task_is_active() -> None:
    session = DurableSession(MemoryDurableStore())
    session.admit_input(
        conversation_id="chat",
        request_id="active",
        task_id="active",
        text="working",
        provider_snapshot={},
    )
    session.start_task("active", state={})
    result = session.admit_write(
        conversation_id="chat",
        request_id="notice-busy",
        kind="notice",
        data={"text": "while busy"},
        display_only=True,
        placement=SubmissionPlacement.QUEUED,
    )

    scheduler = DurableTaskScheduler(session, lambda _task: True)
    scheduler._reconcile()
    scheduler.close()

    assert session.store.submission(str(result.submission_id)).status is SubmissionStatus.DONE
    assert [entry.kind for entry in session.entries("chat")] == ["user_message", "notice"]
    assert session.task("active").status is TaskStatus.RUNNING


def test_head_self_write_cuts_older_context(session: DurableSession) -> None:
    session.create_conversation("chat")
    session.append_message("chat", "user_message", {"text": "old"})

    result = session.admit_write(
        conversation_id="chat",
        request_id="reset-1",
        kind="compaction",
        data={"summary": "fresh range"},
        display_only=False,
        head="self",
    )

    assert session.store.submission(str(result.submission_id)).status is SubmissionStatus.DONE
    snapshot = session.context_snapshot("chat", token_budget=100)
    assert snapshot.entry_ids == (session.entries("chat")[-1].entry_id,)
    assert "old" not in snapshot.entry_ids


def test_queued_head_write_becomes_stale_after_range_reset(session: DurableSession) -> None:
    session.create_conversation("chat")
    old = session.append_message("chat", "user_message", {"text": "old"})
    session.admit_input(
        conversation_id="chat", request_id="active", task_id="active",
        text="working", provider_snapshot={},
    )
    session.start_task("active", state={})
    queued = session.admit_write(
        conversation_id="chat",
        request_id="stale-summary",
        kind="compaction",
        data={"summary": "too old"},
        display_only=False,
        head=old.entry_id,
        placement=SubmissionPlacement.QUEUED,
    )
    session.admit_write(
        conversation_id="chat",
        request_id="reset-1",
        kind="compaction",
        data={"summary": "new range"},
        display_only=False,
        head="self",
    )
    session.complete_task("active", "done", state={})

    scheduler = DurableTaskScheduler(session, lambda _task: True)
    scheduler._reconcile()
    scheduler.close()

    submission = session.store.submission(str(queued.submission_id))
    assert submission.status is SubmissionStatus.UNANSWERED
    assert submission.reason == "stale"
    assert [entry.kind for entry in session.entries("chat")].count("compaction") == 1


def test_head_write_target_in_current_range_places(session: DurableSession) -> None:
    session.create_conversation("chat")
    old = session.append_message("chat", "user_message", {"text": "old"})
    result = session.admit_write(
        conversation_id="chat",
        request_id="summary-1",
        kind="compaction",
        data={"summary": "covers old"},
        display_only=False,
        head=old.entry_id,
        placement=SubmissionPlacement.QUEUED,
    )

    session.place_submission(str(result.submission_id))
    assert session.store.submission(str(result.submission_id)).status is SubmissionStatus.DONE
    assert [entry.kind for entry in session.context("chat")] == ["compaction"]


def test_passive_write_persists_model_and_context_edits(session: DurableSession) -> None:
    session.create_conversation("chat")
    original = session.append_message("chat", "user_message", {"text": "original"})

    result = session.admit_write(
        conversation_id="chat",
        request_id="edit-1",
        kind="context_edit",
        data={"source": "inbox"},
        model={"provider_id": "local", "model_id": "frozen-model"},
        edits=[
            {
                "target": original.entry_id,
                "action": "replace",
                "messages": [{"role": "user", "content": "replacement"}],
            }
        ],
        display_only=False,
    )

    stored = session.store.submission(str(result.submission_id))
    entry = session.entries("chat")[-1]
    assert stored.content is not None
    assert stored.content["model"] == {"provider_id": "local", "model_id": "frozen-model"}
    assert entry.model == {"provider_id": "local", "model_id": "frozen-model"}
    assert entry.head is None
    assert thaw_json(entry.payload["edits"]) == [
        {
            "target": original.entry_id,
            "action": "replace",
            "messages": [{"role": "user", "content": "replacement"}],
        }
    ]
    assert len(entry.edits) == 1
    assert entry.edits[0]["target"] == original.entry_id
    assert session.context_snapshot("chat", token_budget=100).messages == (
        {"role": "user", "content": "replacement"},
    )


def test_display_only_write_rejects_present_context_metadata(session: DurableSession) -> None:
    with pytest.raises(ValueError, match="display-only"):
        session.admit_write(
            conversation_id="chat",
            request_id="display-model",
            kind="notice",
            data={"text": "visible"},
            display_only=True,
            model={},
        )
    with pytest.raises(ValueError, match="display-only"):
        session.admit_write(
            conversation_id="chat",
            request_id="display-edits",
            kind="notice",
            data={"text": "visible"},
            display_only=True,
            edits=[],
        )


def test_queued_passive_write_keeps_model_and_edits_through_placement(session: DurableSession) -> None:
    session.create_conversation("chat")
    original = session.append_message("chat", "user_message", {"text": "original"})
    result = session.admit_write(
        conversation_id="chat",
        request_id="queued-edit-1",
        kind="context_edit",
        data={},
        model={"provider_id": "local", "model_id": "queued-model"},
        edits=[{"target": original.entry_id, "action": "omit"}],
        display_only=False,
        placement=SubmissionPlacement.QUEUED,
    )

    assert session.store.submission(str(result.submission_id)).content["model"] == {
        "provider_id": "local", "model_id": "queued-model",
    }
    session.place_submission(str(result.submission_id))
    entry = session.entries("chat")[-1]
    assert entry.model == {"provider_id": "local", "model_id": "queued-model"}
    assert thaw_json(entry.payload["edits"]) == [{"target": original.entry_id, "action": "omit"}]


def test_queued_passive_metadata_survives_sqlite_restart(tmp_path: Path) -> None:
    path = tmp_path / "durable.sqlite3"
    first = DurableSession(SQLiteDurableStore(path))
    first.create_conversation("chat")
    original = first.append_message("chat", "user_message", {"text": "original"})
    result = first.admit_write(
        conversation_id="chat",
        request_id="restart-edit-1",
        kind="context_edit",
        data={},
        model={"provider_id": "local", "model_id": "restart-model"},
        edits=[{"target": original.entry_id, "action": "omit"}],
        display_only=False,
        placement=SubmissionPlacement.QUEUED,
    )
    first.close()

    second = DurableSession(SQLiteDurableStore(path))
    try:
        queued = second.store.submission(str(result.submission_id))
        assert queued.content is not None
        assert queued.content["model"] == {"provider_id": "local", "model_id": "restart-model"}
        second.place_submission(str(result.submission_id))
        assert second.entries("chat")[-1].model == {
            "provider_id": "local", "model_id": "restart-model",
        }
    finally:
        second.close()


def test_queued_submissions_have_durable_positions_and_withdraw_positionally(
    session: DurableSession,
) -> None:
    session.admit_input(
        conversation_id="chat",
        request_id="active-request",
        task_id="active-task",
        text="working",
        provider_snapshot={},
    )
    session.start_task("active-task", state={})
    first = session.admit_input(
        conversation_id="chat",
        request_id="queued-first",
        task_id="queued-first-task",
        text="first",
        provider_snapshot={},
        placement=SubmissionPlacement.QUEUED,
    )
    write = session.admit_write(
        conversation_id="chat",
        request_id="queued-write",
        kind="note",
        data={"text": "keep later"},
        display_only=False,
        placement=SubmissionPlacement.QUEUED,
    )
    last = session.admit_input(
        conversation_id="chat",
        request_id="queued-last",
        task_id="queued-last-task",
        text="last",
        provider_snapshot={},
        placement=SubmissionPlacement.QUEUED,
    )

    queued = session.queued_submissions("chat")
    assert [item.submission_id for item in queued] == [
        str(first.submission_id), str(write.submission_id), str(last.submission_id),
    ]
    removed = session.withdraw_queued_submission_at(
        "chat", 1, expected_submission_id=str(write.submission_id),
    )
    assert removed.submission_id == str(write.submission_id)
    withdrawn = session.store.submission(str(write.submission_id))
    assert withdrawn.status is SubmissionStatus.UNANSWERED
    assert withdrawn.reason == "aborted"
    assert [item.submission_id for item in session.queued_submissions("chat")] == [
        str(first.submission_id), str(last.submission_id),
    ]
    removed_input = session.withdraw_queued_submission_at("chat", 0)
    assert removed_input.submission_id == str(first.submission_id)
    assert session.store.submission(str(first.submission_id)).status is SubmissionStatus.UNANSWERED
    assert session.store.submission(str(first.submission_id)).reason == "aborted"
    assert session.task("queued-first-task").status is TaskStatus.CANCELLED
    assert [entry.kind for entry in session.entries("chat")] == ["user_message"]


def test_direct_stale_head_write_is_settled_without_an_entry(session: DurableSession) -> None:
    session.create_conversation("chat")
    old = session.append_message("chat", "user_message", {"text": "old"})
    session.admit_write(
        conversation_id="chat",
        request_id="reset-1",
        kind="compaction",
        data={"summary": "new range"},
        display_only=False,
        head="self",
    )

    result = session.admit_write(
        conversation_id="chat",
        request_id="stale-direct",
        kind="compaction",
        data={"summary": "too old"},
        display_only=False,
        head=old.entry_id,
        placement=SubmissionPlacement.PLACED,
    )

    submission = session.store.submission(str(result.submission_id))
    assert submission.status is SubmissionStatus.UNANSWERED
    assert submission.reason == "stale"
    assert len(session.entries("chat")) == 2
    retry = session.admit_write(
        conversation_id="chat",
        request_id="stale-direct",
        kind="compaction",
        data={"summary": "too old"},
        display_only=False,
        head=old.entry_id,
        placement=SubmissionPlacement.PLACED,
    )
    assert retry.sequence == result.sequence
