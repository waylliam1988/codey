"""Queued edits and steering share the durable placement fence."""
from __future__ import annotations

from collections.abc import Callable, Iterator
from concurrent.futures import ThreadPoolExecutor
from contextlib import suppress
from pathlib import Path
from threading import Barrier

import pytest

from codey.app.input_queue import DurableConversationInputQueue, InputItem
from codey.durable import DurableSession, SQLiteDurableStore
from codey.durable.errors import CommitConflict
from codey.task.model import StartTask, task_input_from_payload
from codey.utils.object_fields import mapping_fields
from tests.support.sqlite_store import TemporarySQLiteStore


@pytest.fixture(params=["memory", "sqlite"])
def queue(request: pytest.FixtureRequest, tmp_path: Path) -> Iterator[DurableConversationInputQueue]:
    backend: object = request.param
    store = TemporarySQLiteStore() if backend == "memory" else SQLiteDurableStore(tmp_path / "state.sqlite3")
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


def test_edit_then_steer_preserves_identity_and_consumes_the_edited_text_once(queue: DurableConversationInputQueue) -> None:
    original = queue.get("queued")
    assert original is not None
    updated = queue.edit("queued", original["revision"], "edited")
    assert updated["id"] == original["id"]
    assert updated["origin"] == original["origin"]
    assert updated["request"]["run_id"] == "queued"
    request = queue.durable.task("queued").state["input_request"]
    assert mapping_fields(request) and request["task"] == "original"
    assert queue.durable.task("queued").state["task"] == "edited"
    assert task_input_from_payload(queue.durable.task("queued").state["input"]) == StartTask("edited")
    content = queue.durable.submission(queue.durable.task("queued").submission_id).content
    assert content is not None and content["text"] == "edited"
    assert [entry.payload.get("text") for entry in queue.durable.context("chat")] == ["current"]
    steering = queue.steer("queued", updated["revision"])
    assert steering["request"]["input_mode"] == "steer"
    queue.durable.commit_generation_boundary("chat", task_id="active", at="post_tools")
    consumed = queue.get("queued")
    assert consumed is not None and consumed["state"] == "consumed"
    assert queue.durable.commit_generation_boundary("chat", task_id="active", at="final") == ()
    assert [entry.payload.get("text") for entry in queue.durable.context("chat")] == ["current", "edited"]
    assert len(queue.durable.tasks()) == 2


def test_placement_wins_over_a_late_edit_or_steer(queue: DurableConversationInputQueue) -> None:
    item = queue.get("queued")
    assert item is not None
    queue.durable.complete_task("active", "done", state={})
    queue.durable.place_boundary_inputs("chat", at="final")
    revision = item["revision"]
    operations: tuple[Callable[[], InputItem], ...] = (
        lambda: queue.edit("queued", revision, "too late"),
        lambda: queue.steer("queued", revision),
    )
    for operation in operations:
        with pytest.raises(ValueError, match="revision|placed"):
            operation()
    placed = queue.get("queued")
    assert placed is not None and placed["request"]["task"] == "original"
    assert len([entry for entry in queue.durable.context("chat") if entry.kind == "user_message"]) == 2


def test_stale_revision_does_not_overwrite_another_edit(queue: DurableConversationInputQueue) -> None:
    item = queue.get("queued")
    assert item is not None
    queue.edit("queued", item["revision"], "first edit")
    with pytest.raises(ValueError, match="revision"):
        queue.edit("queued", item["revision"], "stale edit")
    edited = queue.get("queued")
    assert edited is not None and edited["request"]["task"] == "first edit"


@pytest.mark.parametrize("text", ["", " ", "a" * 64001], ids=["empty", "blank", "too-long"])
def test_invalid_edit_changes_nothing(queue: DurableConversationInputQueue, text: str) -> None:
    item = queue.get("queued")
    assert item is not None
    with pytest.raises(ValueError, match="input text"):
        queue.edit("queued", item["revision"], text)
    assert queue.get("queued") == item


def test_edit_and_placement_race_has_one_authoritative_result(queue: DurableConversationInputQueue) -> None:
    item = queue.get("queued")
    assert item is not None
    revision = item["revision"]
    queue.durable.complete_task("active", "done", state={})
    barrier = Barrier(2)

    def edit() -> None:
        barrier.wait()
        with suppress(ValueError):
            queue.edit("queued", revision, "edited")

    def place() -> None:
        barrier.wait()
        queue.durable.place_boundary_inputs("chat", at="final")

    with ThreadPoolExecutor(max_workers=2) as pool:
        operations: tuple[Callable[[], None], ...] = (edit, place)
        futures = [pool.submit(operation) for operation in operations]
        for future in futures:
            future.result()
    entries = [entry for entry in queue.durable.context("chat") if entry.by_task_id == "queued"]
    assert len(entries) == 1
    placed = queue.get("queued")
    assert placed is not None and entries[0].payload["text"] == placed["request"]["task"]
    assert len(queue.durable.tasks()) == 2


def test_store_rejects_revising_a_placed_submission(queue: DurableConversationInputQueue) -> None:
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
