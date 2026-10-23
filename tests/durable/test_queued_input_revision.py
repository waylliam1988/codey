"""Queued edits and steering share the durable placement fence."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from contextlib import suppress
from threading import Barrier

import pytest

from codey.app.input_queue import DurableConversationInputQueue
from codey.durable import DurableSession, MemoryDurableStore, SQLiteDurableStore
from codey.durable.errors import CommitConflict


@pytest.fixture(params=["memory", "sqlite"])
def queue(request, tmp_path):
    store = MemoryDurableStore() if request.param == "memory" else SQLiteDurableStore(tmp_path / "state.sqlite3")
    session = DurableSession(store)
    session.admit_input(conversation_id="chat", request_id="active", task_id="active", text="current", provider_snapshot={})
    session.start_task("active", state={})
    session.admit_input(
        conversation_id="chat", request_id="queued", task_id="queued", text="original", provider_snapshot={},
        placement="queued", task_state={"input_identity": "queued", "input_request": {
            "session_id": "chat", "run_id": "queued", "task": "original", "provider": "local",
        }},
    )
    yield DurableConversationInputQueue(session, emit=lambda event: None)
    session.close()


def test_edit_then_steer_preserves_identity_and_consumes_the_edited_text_once(queue):
    original = queue.get("queued")
    updated = queue.edit("queued", original["revision"], "edited")
    assert updated["id"] == original["id"]
    assert updated["origin"] == original["origin"]
    assert updated["request"]["run_id"] == "queued"
    assert queue.durable.task("queued").state["input_request"]["task"] == "original"
    assert queue.durable.task("queued").state["task"] == "edited"
    assert queue.durable.submission(queue.durable.task("queued").submission_id).content["text"] == "edited"
    assert [entry.payload.get("text") for entry in queue.durable.context("chat")] == ["current"]
    steering = queue.steer("queued", updated["revision"])
    assert steering["request"]["input_mode"] == "steer"
    queue.durable.commit_generation_boundary("chat", task_id="active", at="post_tools")
    assert queue.get("queued")["state"] == "consumed"
    assert queue.durable.commit_generation_boundary("chat", task_id="active", at="final") == ()
    assert [entry.payload.get("text") for entry in queue.durable.context("chat")] == ["current", "edited"]
    assert len(queue.durable.tasks()) == 2


def test_placement_wins_over_a_late_edit_or_steer(queue):
    item = queue.get("queued")
    queue.durable.complete_task("active", "done", state={})
    queue.durable.place_boundary_inputs("chat", at="final")
    for operation in (lambda: queue.edit("queued", item["revision"], "too late"),
                      lambda: queue.steer("queued", item["revision"])):
        with pytest.raises(ValueError, match="revision|placed"):
            operation()
    assert queue.get("queued")["request"]["task"] == "original"
    assert len([entry for entry in queue.durable.context("chat") if entry.kind == "user_message"]) == 2


def test_stale_revision_does_not_overwrite_another_edit(queue):
    item = queue.get("queued")
    queue.edit("queued", item["revision"], "first edit")
    with pytest.raises(ValueError, match="revision"):
        queue.edit("queued", item["revision"], "stale edit")
    assert queue.get("queued")["request"]["task"] == "first edit"


@pytest.mark.parametrize("text", ["", " ", "a" * 64001], ids=["empty", "blank", "too-long"])
def test_invalid_edit_changes_nothing(queue, text):
    item = queue.get("queued")
    with pytest.raises(ValueError, match="input text"):
        queue.edit("queued", item["revision"], text)
    assert queue.get("queued") == item


def test_edit_and_placement_race_has_one_authoritative_result(queue):
    item = queue.get("queued")
    queue.durable.complete_task("active", "done", state={})
    barrier = Barrier(2)

    def edit():
        barrier.wait()
        with suppress(ValueError):
            queue.edit("queued", item["revision"], "edited")

    def place():
        barrier.wait()
        queue.durable.place_boundary_inputs("chat", at="final")

    with ThreadPoolExecutor(max_workers=2) as pool:
        list(pool.map(lambda operation: operation(), (edit, place)))
    entries = [entry for entry in queue.durable.context("chat") if entry.by_task_id == "queued"]
    assert len(entries) == 1
    assert entries[0].payload["text"] == queue.get("queued")["request"]["task"]
    assert len(queue.durable.tasks()) == 2


def test_store_rejects_revising_a_placed_submission(queue):
    from codey.durable.model import DurableMutation, SubmissionUpdate

    task = queue.durable.task("active")
    submission = queue.durable.submission(task.submission_id)
    with pytest.raises((CommitConflict, ValueError), match="queued user inputs|pending task"):
        queue.durable.store.commit(DurableMutation(
            conversation_id="chat", expected_revision=queue.durable.store.head("chat")[1],
            submission_update=SubmissionUpdate(
                submission_id=submission.submission_id, conversation_id="chat",
                expected_status=submission.status, status=submission.status, content={"text": "rewrite"},
            ),
        ))
