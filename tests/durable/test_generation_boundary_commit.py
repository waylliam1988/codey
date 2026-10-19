"""Generation boundary placement and checkpoint share one durable commit."""

from __future__ import annotations

import pytest

from codey.durable import DurableSession, MemoryDurableStore, SubmissionPlacement


def _session() -> tuple[DurableSession, str]:
    session = DurableSession(MemoryDurableStore())
    queued = session.admit_input(
        conversation_id="chat",
        request_id="active",
        task_id="active",
        text="current",
        provider_snapshot={"provider_id": "test"},
    )
    session.start_task("active", state={})
    session.admit_input(
        conversation_id="chat",
        request_id="steer",
        task_id="steer",
        text="change direction",
        provider_snapshot={"provider_id": "test"},
        placement=SubmissionPlacement.QUEUED,
        mode="steer",
    )
    return session, str(queued.submission_id)


def test_generation_boundary_commits_placement_and_active_checkpoint_together() -> None:
    session, _submission_id = _session()
    before = session.commit_sequence()

    placed = session.commit_generation_boundary("chat", task_id="active", at="post_tools")

    assert [item.request_id for item in placed] == ["steer"]
    assert session.commit_sequence() == before + 1
    assert session.task("active").state["generation_boundary"] == "post_tools"
    assert session.store.submission(str(placed[0].submission_id)).placement is SubmissionPlacement.PLACED


def test_generation_boundary_failure_leaves_placement_and_checkpoint_unchanged(monkeypatch: pytest.MonkeyPatch) -> None:
    session, submission_id = _session()
    before_task = session.task("active")
    before_submission = session.store.submission(submission_id)

    def fail(_mutation: object) -> object:
        raise OSError("injected boundary commit failure")

    monkeypatch.setattr(session.store, "commit", fail)
    with pytest.raises(OSError, match="injected boundary"):
        session.commit_generation_boundary("chat", task_id="active", at="post_tools")

    assert session.store.task("active") == before_task
    assert session.store.submission(submission_id) == before_submission
