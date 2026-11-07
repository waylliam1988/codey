from __future__ import annotations

import threading
import time
from collections.abc import Callable
from pathlib import Path

import pytest

from codey.durable import CommitConflict, DurableSession, SQLiteDurableStore, TaskStatus
from codey.durable.model import CommitResult, TaskCheckpoint
from codey.durable.scheduler import DurableTaskScheduler


def _admit(session: DurableSession) -> None:
    session.admit_input(
        conversation_id="chat", request_id="request", task_id="task", text="run",
        provider_snapshot={"provider_id": "local"},
    )


def _wait_for(predicate: Callable[[], bool]) -> bool:
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return predicate()


@pytest.mark.parametrize("dispatch_raises", [False, True])
def test_release_failure_retries_cleanup_before_dispatching_again(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, dispatch_raises: bool,
) -> None:
    session = DurableSession(SQLiteDurableStore(tmp_path / "durable.sqlite3"))
    _admit(session)
    attempts: list[str] = []
    releases: list[str | None] = []
    accepted = threading.Event()
    unclaim = session.unclaim_task

    def dispatch(task: TaskCheckpoint) -> bool:
        attempts.append(task.attempt_token)
        if len(attempts) == 1:
            if dispatch_raises:
                raise OSError("worker queue unavailable")
            return False
        accepted.set()
        return True

    def flaky_unclaim(task_id: str, *, expected_attempt_token: str | None = None) -> CommitResult:
        releases.append(expected_attempt_token)
        if len(releases) == 1:
            raise OSError("temporary release failure")
        return unclaim(task_id, expected_attempt_token=expected_attempt_token)

    monkeypatch.setattr(session, "unclaim_task", flaky_unclaim)
    scheduler = DurableTaskScheduler(session, dispatch, retry_interval=0.01)
    try:
        scheduler.start()
        assert accepted.wait(5), "release failure stranded a running task without a worker"
        assert _wait_for(lambda: not scheduler.in_flight)
        assert len(attempts) == 2
        assert attempts[0] != attempts[1]
        assert releases == [attempts[0], attempts[0]]
        assert session.task("task").attempt_token == attempts[1]
        assert scheduler.enabled
        assert scheduler.blocked_tasks == {}
        assert scheduler.task_failures == {}
    finally:
        assert scheduler.close()
        session.close()


def test_persistent_release_failure_trips_circuit_and_resume_retries_same_token(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    session = DurableSession(SQLiteDurableStore(tmp_path / "durable.sqlite3"))
    _admit(session)
    attempts: list[str] = []
    releases: list[str | None] = []
    publications: list[dict[str, object]] = []
    unclaim = session.unclaim_task

    def dispatch(task: TaskCheckpoint) -> bool:
        attempts.append(task.attempt_token)
        return len(attempts) > 1

    def broken_unclaim(task_id: str, *, expected_attempt_token: str | None = None) -> CommitResult:
        releases.append(expected_attempt_token)
        raise OSError("release storage unavailable")

    scheduler = DurableTaskScheduler(session, dispatch, retry_interval=0.01)
    scheduler.set_health_callback(lambda: publications.append(scheduler.health()))
    monkeypatch.setattr(session, "unclaim_task", broken_unclaim)
    try:
        scheduler.start()
        assert _wait_for(lambda: not scheduler.enabled)
        assert _wait_for(lambda: any(p["enabled"] is False for p in publications[1:]))
        assert len(attempts) == 1
        assert releases == [attempts[0]] * 3
        assert scheduler.blocked_tasks == {"task": "unclaim_failed"}
        assert session.task("task").status is TaskStatus.RUNNING
        failure = scheduler.loop_failure
        assert failure is not None and failure.retry_count == 3
        assert failure.message == "release storage unavailable"

        monkeypatch.setattr(session, "unclaim_task", unclaim)
        scheduler.resume()
        assert _wait_for(lambda: len(attempts) == 2 and not scheduler.in_flight)
        assert scheduler.enabled
        assert scheduler.blocked_tasks == {}
        assert session.task("task").attempt_token == attempts[1]
        assert attempts[0] != attempts[1]
    finally:
        assert scheduler.close()
        session.close()


def test_close_stops_new_dispatches_and_finishes_rejected_claim_cleanup(tmp_path: Path) -> None:
    session = DurableSession(SQLiteDurableStore(tmp_path / "durable.sqlite3"))
    _admit(session)
    session.admit_input(conversation_id="other", request_id="other", task_id="z-other", text="run",
                        provider_snapshot={"provider_id": "local"})
    dispatched = threading.Event()
    attempts: list[str] = []

    def dispatch(task: TaskCheckpoint) -> bool:
        attempts.append(task.task_id)
        invocation = scheduler.invocation(task.task_id)
        assert invocation is not None
        dispatched.set()
        assert invocation.signal.wait(5)
        return False

    scheduler = DurableTaskScheduler(session, dispatch)
    try:
        scheduler.start()
        assert dispatched.wait(5)
        assert scheduler.close()
        assert attempts == ["task"]
        assert session.task("task").status is TaskStatus.PENDING
        assert not session.task("task").attempt_token
        assert session.task("z-other").status is TaskStatus.PENDING
    finally:
        assert scheduler.close()
        session.close()


@pytest.mark.parametrize("outcome", ["later_attempt", "cancelled", "same_attempt_conflict"])
def test_release_retry_respects_durable_ownership_changes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, outcome: str,
) -> None:
    session = DurableSession(SQLiteDurableStore(tmp_path / "durable.sqlite3"))
    _admit(session)
    attempts: list[str] = []
    scheduler = DurableTaskScheduler(session, lambda task: attempts.append(task.attempt_token) or False)
    unclaim = session.unclaim_task
    calls = 0

    def flaky_unclaim(task_id: str, *, expected_attempt_token: str | None = None) -> CommitResult:
        nonlocal calls
        calls += 1
        if calls == 1:
            if outcome == "same_attempt_conflict":
                raise CommitConflict("transient revision conflict")
            raise OSError("temporary release failure")
        return unclaim(task_id, expected_attempt_token=expected_attempt_token)

    monkeypatch.setattr(session, "unclaim_task", flaky_unclaim)
    try:
        with pytest.raises(CommitConflict if outcome == "same_attempt_conflict" else OSError):
            scheduler._reconcile()
        token = session.task("task").attempt_token
        assert attempts == [token]
        if outcome == "later_attempt":
            unclaim("task", expected_attempt_token=token)
            later = session.claim_task_checkpoint("task")
        elif outcome == "cancelled":
            session.cancel_task("task", reason="stopped")
        else:
            # Complete cleanup without a new dispatch in this pass.
            scheduler.claim("task")
        scheduler._reconcile()
        assert scheduler.blocked_tasks == {}
        current = session.task("task")
        if outcome == "later_attempt":
            assert current.attempt_token == later.attempt_token
            assert current.status is TaskStatus.RUNNING
        elif outcome == "cancelled":
            assert current.status is TaskStatus.CANCELLED
        else:
            assert current.status is TaskStatus.PENDING
            assert not current.attempt_token
    finally:
        assert scheduler.close()
        session.close()
