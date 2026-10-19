from dataclasses import replace
from pathlib import Path

import pytest

from codey.durable import (
    CommitConflict,
    DurableMutation,
    DurableSession,
    MemoryDurableStore,
    SQLiteDurableStore,
    SubmissionStatus,
)


@pytest.fixture(params=["memory", "sqlite"])
def session(request: pytest.FixtureRequest, tmp_path: Path):
    store = MemoryDurableStore() if request.param == "memory" else SQLiteDurableStore(tmp_path / "durable.sqlite3")
    durable = DurableSession(store)
    durable.admit_input(conversation_id="chat", request_id="request", task_id="task", text="inspect",
                        provider_snapshot={"provider_id": "local"})
    yield durable
    durable.close()


def test_queued_failure_is_one_terminal_commit(session: DurableSession) -> None:
    before = session.commit_sequence()
    session.fail_task("task", "worker could not start", state={})
    task = session.task("task")
    assert task.phase == "failed"
    assert session.store.submission(task.submission_id).status is SubmissionStatus.UNANSWERED
    assert session.commit_sequence() == before + 1


def test_advancement_preserves_admitted_input_and_authorization(session: DurableSession) -> None:
    session.record_task_policy("task", {"mode": "read_only"})
    session.record_reviewer_selection("task", {"provider_id": "reviewer"})
    admitted = session.task("task")
    session.start_task("task", state={"step": 1})
    task = session.task("task")
    for key in ("task_policy", "reviewer_selection", "input_entry_id"):
        assert task.state[key] == admitted.state[key]
    assert task.state["step"] == 1


def test_nested_frozen_authorization_survives_checkpoint_and_failure(session: DurableSession) -> None:
    policy = {"capabilities": ["project.read"], "checks": {"required": ["verify"]}}
    session.record_task_policy("task", policy)
    session.start_task("task", state={})
    session.fail_task("task", "stopped", state={})
    assert session.task("task").state["task_policy"]["capabilities"] == ("project.read",)


def test_history_exposes_committed_identity_for_live_projection_reconciliation(session: DurableSession) -> None:
    session.complete_task("task", "answer", state={})
    page = session.history_page("chat")
    assert [(item["type"], item["runId"]) for item in page["items"]] == [("user", "task"), ("asst", "task")]
    assert page["items"][1]["finalAnswer"] is True


@pytest.mark.parametrize("key", ["task_policy", "reviewer_selection", "input_entry_id"])
@pytest.mark.parametrize("change", ["replace", "remove"])
def test_low_level_commit_cannot_rewrite_frozen_task_authority(session: DurableSession, key: str, change: str) -> None:
    session.record_task_policy("task", {"mode": "read_only"})
    session.record_reviewer_selection("task", {"provider_id": "reviewer"})
    current = session.task("task")
    state = dict(current.state)
    if change == "remove":
        state.pop(key)
    else:
        state[key] = "replacement" if key == "input_entry_id" else {"mode": "unrestricted"}
    _, revision = session.head("chat")
    before = session.commit_sequence()
    with pytest.raises(CommitConflict, match="immutable"):
        session.store.commit(DurableMutation("chat", revision, task=replace(current, revision=current.revision + 1, state=state)))
    assert session.task("task") == current
    assert session.commit_sequence() == before
