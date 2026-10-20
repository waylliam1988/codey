from __future__ import annotations

import threading
import time
from pathlib import Path

from codey.durable import CommitConflict, DurableSession, SQLiteDurableStore, TaskStatus
from codey.durable.scheduler import DurableTaskScheduler
from codey.durable.workflow import WorkflowDefinition


def _admit(session: DurableSession, task_id: str) -> None:
    session.admit_input(
        conversation_id="conversation-1",
        request_id=f"request-{task_id}",
        task_id=task_id,
        text=f"task {task_id}",
        provider_snapshot={"provider_id": "local", "model_id": "test"},
    )


def _wait_for(predicate, timeout: float = 2.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return predicate()


def test_scheduler_retries_durable_task_when_worker_queue_is_full(tmp_path: Path) -> None:
    session = DurableSession(SQLiteDurableStore(tmp_path / "durable.sqlite3"))
    _admit(session, "task-1")
    attempts: list[str] = []
    accepted = threading.Event()

    def dispatch(task) -> bool:
        attempts.append(task.task_id)
        if len(attempts) == 1:
            return False
        session.start_task(task.task_id, state={"next": "run"})
        accepted.set()
        return True

    scheduler = DurableTaskScheduler(session, dispatch, retry_interval=0.01)
    try:
        scheduler._reconcile()
        assert attempts == ["task-1"]
        assert session.task("task-1").status == TaskStatus.PENDING
        assert not session.task("task-1").attempt_token
        scheduler._reconcile()
        assert accepted.is_set()
        assert attempts[:2] == ["task-1", "task-1"]
        assert session.task("task-1").phase == "provider_prepared"
    finally:
        scheduler.close()
        session.close()


def test_scheduler_blocks_workflow_without_a_compatible_definition(tmp_path: Path) -> None:
    session = DurableSession(SQLiteDurableStore(tmp_path / "durable.sqlite3"))
    session.admit_input(
        conversation_id="conversation-1",
        request_id="request-1",
        task_id="task-1",
        text="workflow",
        provider_snapshot={"provider_id": "local"},
        workflow_name="counter",
        workflow_version=2,
        workflow_input={"limit": 3},
        workflow_checkpoint={"value": 1},
    )
    dispatched: list[str] = []
    scheduler = DurableTaskScheduler(
        session,
        lambda task: dispatched.append(task.task_id) or True,
        workflows={"counter": WorkflowDefinition(name="counter", version=1)},
    )
    try:
        scheduler._reconcile()
        assert dispatched == []
        assert session.task("task-1").status == TaskStatus.PENDING
    finally:
        scheduler.close()
        session.close()


def test_scheduler_migrates_workflow_before_dispatch(tmp_path: Path) -> None:
    session = DurableSession(SQLiteDurableStore(tmp_path / "durable.sqlite3"))
    session.admit_input(
        conversation_id="conversation-1",
        request_id="request-1",
        task_id="task-1",
        text="workflow",
        provider_snapshot={"provider_id": "local"},
        workflow_name="counter",
        workflow_version=1,
        workflow_input={"limit": 3},
        workflow_checkpoint={"value": 1},
    )
    dispatched: list[str] = []
    scheduler = DurableTaskScheduler(
        session,
        lambda task: dispatched.append(task.task_id) or True,
        workflows={
            "counter": WorkflowDefinition(
                name="counter",
                version=2,
                migrate=lambda checkpoint: {**checkpoint, "migrated": True},
            )
        },
    )
    try:
        scheduler._reconcile()
        assert dispatched == ["task-1"]
        task = session.task("task-1")
        assert task.workflow_version == 2
        assert task.workflow_checkpoint["migrated"] is True
    finally:
        scheduler.close()
        session.close()


def test_scheduler_reopens_and_dispatches_accepted_tasks_once(tmp_path: Path) -> None:
    path = tmp_path / "durable.sqlite3"
    first = DurableSession(SQLiteDurableStore(path))
    _admit(first, "task-1")
    first.close()

    restarted = DurableSession(SQLiteDurableStore(path))
    dispatched: list[str] = []
    done = threading.Event()

    def dispatch(task) -> bool:
        dispatched.append(task.task_id)
        restarted.start_task(task.task_id, state={"next": "run"})
        done.set()
        return True

    scheduler = DurableTaskScheduler(restarted, dispatch, retry_interval=0.01)
    scheduler.start()
    try:
        assert done.wait(2)
        assert _wait_for(lambda: scheduler.in_flight == ())
        assert dispatched == ["task-1"]
    finally:
        scheduler.close()
        restarted.close()


def test_scheduler_reopens_nonterminal_running_task_for_recovery(tmp_path: Path) -> None:
    path = tmp_path / "durable.sqlite3"
    first = DurableSession(SQLiteDurableStore(path))
    _admit(first, "task-1")
    first.start_task("task-1", state={"next": "resume"})
    first.advance_task(task_id="task-1", phase="assistant_response_pending", state={"next": "resume"})
    first.close()

    restarted = DurableSession(SQLiteDurableStore(path))
    dispatched: list[tuple[str, str]] = []
    done = threading.Event()

    def dispatch(task) -> bool:
        dispatched.append((task.task_id, task.phase))
        done.set()
        return True

    scheduler = DurableTaskScheduler(restarted, dispatch, retry_interval=0.01)
    scheduler.start()
    try:
        assert done.wait(2)
        assert dispatched == [("task-1", "assistant_response_pending")]
        assert restarted.task("task-1").status == TaskStatus.RUNNING
    finally:
        scheduler.close()
        restarted.close()


def test_scheduler_stops_polling_when_no_durable_tasks_remain(tmp_path: Path) -> None:
    session = DurableSession(SQLiteDurableStore(tmp_path / "durable.sqlite3"))
    scheduler = DurableTaskScheduler(session, lambda _task: True, retry_interval=0.01)
    scheduler.start()
    assert _wait_for(lambda: scheduler._thread is None or not scheduler._thread.is_alive())
    assert scheduler.close()
    session.close()


def test_scheduler_restarts_after_becoming_idle_before_a_later_commit(tmp_path: Path) -> None:
    session = DurableSession(SQLiteDurableStore(tmp_path / "durable.sqlite3"))
    dispatched: list[str] = []
    scheduler = DurableTaskScheduler(
        session,
        lambda task: dispatched.append(task.task_id) or True,
        retry_interval=0.01,
    )
    scheduler.start()
    try:
        assert _wait_for(lambda: scheduler._thread is None or not scheduler._thread.is_alive())
        _admit(session, "task-late")
        scheduler.notify()
        assert _wait_for(lambda: dispatched == ["task-late"])
    finally:
        scheduler.close()
        session.close()


def test_waiting_task_is_not_redispatched_until_explicitly_requeued(tmp_path: Path) -> None:
    session = DurableSession(SQLiteDurableStore(tmp_path / "durable.sqlite3"))
    _admit(session, "task-wait")
    session.claim_task("task-wait")
    session.start_task("task-wait", state={"next": "approval"})
    session.wait_task("task-wait", state={"wait_reason": "approval"})
    seen: list[str] = []
    scheduler = DurableTaskScheduler(session, lambda task: seen.append(task.task_id) or True, retry_interval=0.01)
    try:
        scheduler._reconcile()
        assert seen == []
        assert session.task("task-wait").status == TaskStatus.WAITING
        session.advance_task(task_id="task-wait", phase="provider_prepared", state={"next": "resume"})
        session.unclaim_task("task-wait")
        scheduler.notify("task-wait")
        scheduler._reconcile()
        assert seen == ["task-wait"]
    finally:
        scheduler.close()
        session.close()


def test_cancel_request_is_a_durable_dispatch_barrier(tmp_path: Path) -> None:
    session = DurableSession(SQLiteDurableStore(tmp_path / "durable.sqlite3"))
    _admit(session, "task-cancel")
    session.request_cancel("task-cancel", reason="user stopped")
    dispatched: list[str] = []
    scheduler = DurableTaskScheduler(
        session, lambda task: dispatched.append(task.task_id) or True, retry_interval=0.01
    )
    scheduler.start()
    try:
        assert _wait_for(lambda: session.task("task-cancel").status == TaskStatus.CANCELLED)
        assert dispatched == []
        assert session.task("task-cancel").state["cancel_reason"] == "user stopped"
    finally:
        scheduler.close()
        session.close()


def test_cancel_barrier_rejects_old_worker_progress(tmp_path: Path) -> None:
    session = DurableSession(SQLiteDurableStore(tmp_path / "durable.sqlite3"))
    _admit(session, "task-cancel")
    session.claim_task("task-cancel")
    session.start_task("task-cancel", state={"next": "run"})
    token = session.task("task-cancel").attempt_token
    session.request_cancel("task-cancel", reason="stop")
    with session.execution("task-cancel", token):
        try:
            session.advance_task(task_id="task-cancel", phase="assistant_intent_committed", state={})
        except CommitConflict:
            pass
        else:
            raise AssertionError("cancelled work accepted a non-terminal worker mutation")
    session.cancel_task("task-cancel", reason="stop")
    assert session.task("task-cancel").status == TaskStatus.CANCELLED
    session.close()


def test_restart_does_not_redispatch_running_task_with_cancel_intent(tmp_path: Path) -> None:
    path = tmp_path / "durable.sqlite3"
    first = DurableSession(SQLiteDurableStore(path))
    _admit(first, "task-cancel")
    first.claim_task("task-cancel")
    first.start_task("task-cancel", state={"next": "run"})
    first.request_cancel("task-cancel", reason="power button")
    first.close()

    restarted = DurableSession(SQLiteDurableStore(path))
    # This test simulates a process restart inside one pytest process. The
    # production recovery path protects live PIDs, so use explicit
    # administrative recovery for the simulated crash boundary.
    restarted.recover_scheduler_state(force=True)
    dispatched: list[str] = []
    scheduler = DurableTaskScheduler(
        restarted, lambda task: dispatched.append(task.task_id) or True, retry_interval=0.01
    )
    scheduler.start()
    try:
        assert _wait_for(lambda: restarted.task("task-cancel").status == TaskStatus.CANCELLED)
        assert dispatched == []
    finally:
        scheduler.close()
        restarted.close()
