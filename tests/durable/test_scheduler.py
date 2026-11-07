from __future__ import annotations

import threading
import time
from pathlib import Path

import codey.durable.scheduler as scheduler_module
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
        assert session.task("task-1").phase == "running"
    finally:
        scheduler.close()
        session.close()


def test_scheduler_bounds_reconcile_failures_and_exposes_cause(tmp_path: Path, monkeypatch) -> None:
    session = DurableSession(SQLiteDurableStore(tmp_path / "durable.sqlite3"))
    _admit(session, "task-1")
    scheduler = DurableTaskScheduler(session, lambda _task: True, retry_interval=0.01)
    calls = 0

    def broken_reconcile() -> None:
        nonlocal calls
        calls += 1
        raise OSError("injected reconcile failure")

    monkeypatch.setattr(scheduler, "_reconcile", broken_reconcile)
    try:
        scheduler.start()
        assert _wait_for(lambda: scheduler.loop_failure is not None and scheduler._thread is None)
        failure = scheduler.loop_failure
        assert failure is not None
        assert calls == 3
        assert failure.phase == "reconcile"
        assert failure.error_type == "OSError"
        assert failure.message == "injected reconcile failure"
        assert failure.retry_count == 3
    finally:
        scheduler.close()
        session.close()


def test_scheduler_start_resumes_after_reconcile_circuit_breaker(tmp_path: Path, monkeypatch) -> None:
    session = DurableSession(SQLiteDurableStore(tmp_path / "durable.sqlite3"))
    _admit(session, "task-1")
    dispatched: list[str] = []
    scheduler = DurableTaskScheduler(
        session,
        lambda task: dispatched.append(task.task_id) or True,
        retry_interval=0.01,
    )
    original_reconcile = scheduler._reconcile
    calls = 0

    def intermittently_broken_reconcile() -> None:
        nonlocal calls
        calls += 1
        if calls <= 3:
            raise OSError("injected reconcile failure")
        original_reconcile()

    monkeypatch.setattr(scheduler, "_reconcile", intermittently_broken_reconcile)
    try:
        scheduler.start()
        assert _wait_for(lambda: scheduler.loop_failure is not None and not scheduler.enabled)
        assert scheduler._owns_scheduler
        scheduler.start()
        assert _wait_for(lambda: dispatched == ["task-1"])
        assert scheduler.enabled
    finally:
        scheduler.close()
        session.close()


def test_scheduler_resume_replaces_a_thread_stuck_in_circuit_breaker_logging(tmp_path: Path, monkeypatch) -> None:
    session = DurableSession(SQLiteDurableStore(tmp_path / "durable.sqlite3"))
    _admit(session, "task-1")
    dispatched: list[str] = []
    scheduler = DurableTaskScheduler(
        session,
        lambda task: dispatched.append(task.task_id) or True,
        retry_interval=0.01,
    )
    original_reconcile = scheduler._reconcile
    calls = 0
    entered_log = threading.Event()
    release_log = threading.Event()

    def intermittently_broken_reconcile() -> None:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise OSError("injected circuit breaker failure")
        original_reconcile()

    def blocked_error(*args, **kwargs) -> None:
        entered_log.set()
        assert release_log.wait(2)

    monkeypatch.setattr(scheduler, "_reconcile", intermittently_broken_reconcile)
    monkeypatch.setattr(scheduler_module._LOGGER, "error", blocked_error)
    scheduler._loop_failure_limit = 1
    try:
        scheduler.start()
        assert entered_log.wait(2)
        scheduler.resume()
        assert _wait_for(lambda: dispatched == ["task-1"])
        assert not scheduler.close(timeout=0.01)
        release_log.set()
        assert scheduler.close(timeout=2)
    finally:
        release_log.set()
        scheduler.close()
        session.close()


def test_scheduler_health_publication_failure_does_not_rollback_worker(tmp_path: Path) -> None:
    session = DurableSession(SQLiteDurableStore(tmp_path / "durable.sqlite3"))
    _admit(session, "task-1")
    dispatched = threading.Event()
    scheduler = DurableTaskScheduler(session, lambda _task: dispatched.set() or True, retry_interval=0.01)

    def broken_health_publication() -> None:
        raise OSError("injected health publication failure")

    try:
        scheduler.set_health_callback(broken_health_publication)
        scheduler.start()
        assert dispatched.wait(2)
        assert scheduler.enabled
        assert scheduler._owns_scheduler
    finally:
        scheduler.close()
        session.close()


def test_scheduler_requeues_after_read_failure_and_retries_automatically(tmp_path: Path, monkeypatch) -> None:
    session = DurableSession(SQLiteDurableStore(tmp_path / "durable.sqlite3"))
    _admit(session, "task-1")
    dispatched: list[str] = []
    original_task = session.task
    failed = True

    def flaky_task(task_id: str):
        nonlocal failed
        if failed:
            failed = False
            raise OSError("injected task read failure")
        return original_task(task_id)

    monkeypatch.setattr(session, "task", flaky_task)
    scheduler = DurableTaskScheduler(
        session,
        lambda task: dispatched.append(task.task_id) or True,
        retry_interval=0.01,
    )
    try:
        scheduler.start()
        assert _wait_for(lambda: dispatched == ["task-1"])
        assert scheduler.task_failures == {}
        assert session.task("task-1").status is TaskStatus.RUNNING
    finally:
        scheduler.close()
        session.close()


def test_scheduler_requeues_after_claim_failure(tmp_path: Path, monkeypatch) -> None:
    session = DurableSession(SQLiteDurableStore(tmp_path / "durable.sqlite3"))
    _admit(session, "task-1")
    scheduler = DurableTaskScheduler(session, lambda _task: True)
    original_claim = session.claim_task_checkpoint
    failed = True

    def flaky_claim(task_id: str):
        nonlocal failed
        if failed:
            failed = False
            raise OSError("injected claim failure")
        return original_claim(task_id)

    monkeypatch.setattr(session, "claim_task_checkpoint", flaky_claim)
    try:
        scheduler._reconcile()
        assert scheduler.task_failures["task-1"].phase == "claim"
        assert "task-1" in scheduler._pending
        scheduler._reconcile()
        assert session.task("task-1").status is TaskStatus.RUNNING
    finally:
        scheduler.close()
        session.close()


def test_scheduler_releases_claim_without_a_post_claim_read(tmp_path: Path, monkeypatch) -> None:
    session = DurableSession(SQLiteDurableStore(tmp_path / "durable.sqlite3"))
    _admit(session, "task-1")
    scheduler = DurableTaskScheduler(session, lambda _task: False)
    original_task = session.task
    reads = 0

    def flaky_task(task_id: str):
        nonlocal reads
        reads += 1
        if reads == 2:
            raise AssertionError("cleanup must use the claimed attempt token")
        return original_task(task_id)

    monkeypatch.setattr(session, "task", flaky_task)
    try:
        scheduler._reconcile()
        task = original_task("task-1")
        assert task.status is TaskStatus.PENDING
        assert not task.attempt_token
        assert "task-1" in scheduler._pending
    finally:
        scheduler.close()
        session.close()


def test_scheduler_keeps_accepted_attempt_when_coordination_read_fails(tmp_path: Path, monkeypatch) -> None:
    session = DurableSession(SQLiteDurableStore(tmp_path / "durable.sqlite3"))
    _admit(session, "task-1")
    dispatched: list[str] = []
    original_task = session.task
    reads = 0

    def flaky_task(task_id: str):
        nonlocal reads
        reads += 1
        if reads == 2:
            raise OSError("injected accepted coordination read failure")
        return original_task(task_id)

    monkeypatch.setattr(session, "task", flaky_task)
    scheduler = DurableTaskScheduler(session, lambda task: dispatched.append(task.task_id) or True)
    try:
        scheduler._reconcile()
        assert dispatched == ["task-1"]
        assert scheduler.task_failures["task-1"].phase == "accepted"
        assert "task-1" in scheduler._scheduled
        assert "task-1" not in scheduler._pending
        assert original_task("task-1").status is TaskStatus.RUNNING
        scheduler._reconcile()
        assert dispatched == ["task-1"]
    finally:
        scheduler.close()
        session.close()


def test_scheduler_cleanup_does_not_release_a_later_attempt(tmp_path: Path) -> None:
    session = DurableSession(SQLiteDurableStore(tmp_path / "durable.sqlite3"))
    _admit(session, "task-1")
    scheduler = DurableTaskScheduler(session, lambda _task: True)
    try:
        first = session.claim_task_checkpoint("task-1")
        session.unclaim_task("task-1", expected_attempt_token=first.attempt_token)
        second = session.claim_task_checkpoint("task-1")
        scheduler._unclaim_if_owned("task-1", first.attempt_token)
        current = session.task("task-1")
        assert current.status is TaskStatus.RUNNING
        assert current.attempt_token == second.attempt_token
    finally:
        scheduler.close()
        session.close()


def test_scheduler_records_dispatch_value_error_and_releases_claim(tmp_path: Path) -> None:
    session = DurableSession(SQLiteDurableStore(tmp_path / "durable.sqlite3"))
    _admit(session, "task-1")
    scheduler = DurableTaskScheduler(
        session,
        lambda _task: (_ for _ in ()).throw(ValueError("missing dispatch identity")),
    )
    try:
        for _ in range(3):
            scheduler._reconcile()
        task = session.task("task-1")
        assert task.status is TaskStatus.FAILED
        assert not task.attempt_token
        assert scheduler.blocked_tasks["task-1"] == "dispatch_failed"
        assert task.state["scheduler_failure"].startswith("scheduler dispatch failed")
    finally:
        scheduler.close()
        session.close()


def test_scheduler_retries_when_failure_commit_conflicts(tmp_path: Path, monkeypatch) -> None:
    session = DurableSession(SQLiteDurableStore(tmp_path / "durable.sqlite3"))
    _admit(session, "task-1")
    scheduler = DurableTaskScheduler(
        session,
        lambda _task: (_ for _ in ()).throw(OSError("worker unavailable")),
    )

    def conflicting_failure(*args, **kwargs):
        raise CommitConflict("injected failure commit conflict")

    monkeypatch.setattr(session, "fail_task", conflicting_failure)
    try:
        for _ in range(3):
            scheduler._reconcile()
        task = session.task("task-1")
        assert task.status is TaskStatus.PENDING
        assert not task.attempt_token
        assert scheduler.blocked_tasks == {}
    finally:
        scheduler.close()
        session.close()


def test_scheduler_waits_between_reconcile_retries(tmp_path: Path, monkeypatch) -> None:
    session = DurableSession(SQLiteDurableStore(tmp_path / "durable.sqlite3"))
    _admit(session, "task-1")
    scheduler = DurableTaskScheduler(session, lambda _task: True, retry_interval=0.05)
    timestamps: list[float] = []

    def broken_reconcile() -> None:
        timestamps.append(time.monotonic())
        raise OSError("injected reconcile failure")

    monkeypatch.setattr(scheduler, "_reconcile", broken_reconcile)
    try:
        scheduler.start()
        assert _wait_for(lambda: len(timestamps) == 3, timeout=2)
        assert timestamps[1] - timestamps[0] >= 0.04
        assert timestamps[2] - timestamps[1] >= 0.04
    finally:
        scheduler.close()
        session.close()


def test_scheduler_waits_after_internal_notify_before_reconcile_retry(tmp_path: Path, monkeypatch) -> None:
    session = DurableSession(SQLiteDurableStore(tmp_path / "durable.sqlite3"))
    _admit(session, "task-1")
    scheduler = DurableTaskScheduler(session, lambda _task: True, retry_interval=0.05)
    timestamps: list[float] = []
    original_finalization_queue = session.store.finalization_queue
    calls = 0

    def broken_finalization_queue():
        nonlocal calls
        calls += 1
        if calls == 1:
            return original_finalization_queue()
        timestamps.append(time.monotonic())
        raise OSError("injected finalization read failure")

    monkeypatch.setattr(session.store, "finalization_queue", broken_finalization_queue)
    try:
        scheduler.start()
        assert _wait_for(lambda: len(timestamps) == 3, timeout=2)
        assert timestamps[1] - timestamps[0] >= 0.04
        assert timestamps[2] - timestamps[1] >= 0.04
    finally:
        scheduler.close()
        session.close()


def test_scheduler_fails_task_after_repeated_dispatch_errors(tmp_path: Path) -> None:
    session = DurableSession(SQLiteDurableStore(tmp_path / "durable.sqlite3"))
    _admit(session, "task-1")
    scheduler = DurableTaskScheduler(session, lambda _task: (_ for _ in ()).throw(OSError("worker unavailable")))
    try:
        for _ in range(3):
            scheduler._reconcile()
        task = session.task("task-1")
        assert task.status is TaskStatus.FAILED
        failure = scheduler.task_failures["task-1"]
        assert failure.phase == "dispatch"
        assert failure.error_type == "OSError"
        assert failure.retry_count == 3
        assert scheduler.blocked_tasks["task-1"] == "dispatch_failed"
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
        # This checks eventual dispatch and exact ownership, not a 2 s storage
        # SLA. Windows SQLite commits exceeded that window in repeated runs
        # even though the callback ran once and reached provider_prepared.
        assert done.wait(10), {
            "dispatched": dispatched,
            "checkpoint": restarted.task("task-1"),
            "in_flight": scheduler.in_flight,
            "blocked": scheduler.blocked_tasks,
        }
        assert _wait_for(lambda: scheduler.in_flight == (), timeout=10)
        assert dispatched == ["task-1"]
    finally:
        scheduler.close()
        restarted.close()


def test_scheduler_reopens_nonterminal_running_task_for_recovery(tmp_path: Path) -> None:
    path = tmp_path / "durable.sqlite3"
    first = DurableSession(SQLiteDurableStore(path))
    _admit(first, "task-1")
    first.start_task("task-1", state={"next": "resume"})
    first.advance_task(task_id="task-1", phase="running", state={"next": "resume"})
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
        assert dispatched == [("task-1", "running")]
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
        session.advance_task(task_id="task-wait", phase="running", state={"next": "resume"})
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
            session.advance_task(task_id="task-cancel", phase="running", state={})
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
