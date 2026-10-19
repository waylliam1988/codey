from __future__ import annotations

from pathlib import Path

import pytest

from codey.durable import DurableSession, SQLiteDurableStore, TaskStatus


def _admit(
    session: DurableSession,
    task_id: str,
    request_id: str,
    *,
    owner_task_id: str | None = None,
    abandon_on_restart: bool = False,
) -> None:
    session.admit_input(
        conversation_id="conversation-1",
        request_id=request_id,
        task_id=task_id,
        text=task_id,
        provider_snapshot={"provider_id": "test", "model_id": "test"},
        owner_task_id=owner_task_id,
        abandon_on_restart=abandon_on_restart,
    )


def test_task_ownership_is_durable_and_validated_after_restart(tmp_path: Path) -> None:
    path = tmp_path / "durable.sqlite3"
    session = DurableSession(SQLiteDurableStore(path))
    _admit(session, "parent", "request-parent")
    _admit(session, "child", "request-child", owner_task_id="parent")

    assert session.task("child").owner_task_id == "parent"
    session.close()

    restarted = DurableSession(SQLiteDurableStore(path))
    try:
        assert restarted.task("child").owner_task_id == "parent"
        with pytest.raises(ValueError, match="owner task"):
            restarted.admit_input(
                conversation_id="conversation-1",
                request_id="request-missing-owner",
                task_id="orphan",
                text="orphan",
                provider_snapshot={},
                owner_task_id="does-not-exist",
            )
    finally:
        restarted.close()


def test_waiting_task_join_policy_requeues_after_children_settle(tmp_path: Path) -> None:
    session = DurableSession(SQLiteDurableStore(tmp_path / "durable.sqlite3"))
    try:
        _admit(session, "parent", "request-parent")
        _admit(session, "child-a", "request-a", owner_task_id="parent")
        _admit(session, "child-b", "request-b", owner_task_id="parent")
        session.wait_task("parent", state={"next": "join"}, on=("child-a", "child-b"), join_policy="allSettled")
        assert session.task("parent").status == TaskStatus.WAITING
        assert session.task("parent").waiting_on == ("child-a", "child-b")

        session.complete_task("child-a", "a", state={})
        session.reconcile_waiting_tasks()
        assert session.task("parent").status == TaskStatus.WAITING

        session.complete_task("child-b", "b", state={})
        session.reconcile_waiting_tasks()
        assert session.task("parent").status == TaskStatus.PENDING
        assert session.task("parent").waiting_on == ()
    finally:
        session.close()


def test_fail_fast_waiting_parent_fails_when_child_fails(tmp_path: Path) -> None:
    session = DurableSession(SQLiteDurableStore(tmp_path / "durable.sqlite3"))
    try:
        _admit(session, "parent", "request-parent")
        _admit(session, "child", "request-child", owner_task_id="parent")
        session.wait_task("parent", state={"next": "join"}, on=("child",), join_policy="failFast")
        session.fail_task("child", "child failed", state={})
        session.reconcile_waiting_tasks()
        assert session.task("parent").status == TaskStatus.FAILED
        assert session.store.submission(session.task("parent").submission_id).reason == "child failed"
    finally:
        session.close()


def test_parent_cancellation_cascades_to_owned_children(tmp_path: Path) -> None:
    session = DurableSession(SQLiteDurableStore(tmp_path / "durable.sqlite3"))
    try:
        _admit(session, "parent", "request-parent")
        _admit(session, "pending", "request-pending", owner_task_id="parent")
        _admit(session, "running", "request-running", owner_task_id="parent")
        session.claim_task("running")
        session.start_task("running", state={"next": "work"})

        session.request_cancel("parent", reason="user stopped")
        assert session.task("parent").status == TaskStatus.WAITING
        assert session.task("pending").status == TaskStatus.CANCELLED
        assert session.task("running").state["cancel_requested"] is True
        assert session.task("running").status == TaskStatus.RUNNING

        session.cancel_task("running", reason="user stopped")
        assert session.task("running").status == TaskStatus.CANCELLED
        session.reconcile_finalization()
        assert session.task("parent").status == TaskStatus.CANCELLED
    finally:
        session.close()


def test_parent_completion_is_held_until_owned_child_finalizes(tmp_path: Path) -> None:
    session = DurableSession(SQLiteDurableStore(tmp_path / "durable.sqlite3"))
    try:
        _admit(session, "parent", "request-parent")
        _admit(session, "child", "request-child", owner_task_id="parent")
        session.complete_task("parent", "answer", state={})
        assert session.task("parent").status == TaskStatus.COMPLETING
        assert session.task("parent").phase == "completion_pending"

        session.complete_task("child", "child answer", state={})
        session.reconcile_finalization()
        assert session.task("parent").status == TaskStatus.COMPLETED
        assert session.task("parent").phase == "completed"
    finally:
        session.close()


def test_waiting_join_survives_restart_and_reconciles_from_durable_children(tmp_path: Path) -> None:
    path = tmp_path / "durable.sqlite3"
    session = DurableSession(SQLiteDurableStore(path))
    _admit(session, "parent", "request-parent")
    _admit(session, "child", "request-child", owner_task_id="parent")
    session.wait_task("parent", state={"next": "join"}, on=("child",), join_policy="allSettled")
    session.close()

    restarted = DurableSession(SQLiteDurableStore(path))
    try:
        assert restarted.task("parent").status == TaskStatus.WAITING
        restarted.complete_task("child", "child answer", state={})
        restarted.reconcile_finalization()
        assert restarted.task("parent").status == TaskStatus.PENDING
        assert restarted.task("parent").waiting_on == ()
    finally:
        restarted.close()


def test_abandon_on_restart_is_terminalized_without_redispatch(tmp_path: Path) -> None:
    path = tmp_path / "durable.sqlite3"
    session = DurableSession(SQLiteDurableStore(path))
    _admit(session, "task", "request", abandon_on_restart=True)
    session.claim_task("task")
    session.start_task("task", state={"next": "work"})
    session.close()

    restarted = DurableSession(SQLiteDurableStore(path))
    try:
        restarted.recover_scheduler_state()
        task = restarted.task("task")
        assert task.phase == "failed"
        assert task.status == TaskStatus.FAILED
        assert restarted.store.submission(task.submission_id).reason == "abandoned on restart"
    finally:
        restarted.close()
