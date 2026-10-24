from __future__ import annotations

import inspect
import sys
import threading
from pathlib import Path

from codey.durable import DurableSession, SQLiteDurableStore, TaskStatus
from codey.durable.scheduler import DurableTaskScheduler


def _admit(session, task_id, *, queued=False):
    session.admit_input(
        conversation_id="chat",
        request_id=f"request-{task_id}",
        task_id=task_id,
        text=task_id,
        provider_snapshot={"provider_id": "local"},
        task_state={"input_state": "queued"} if queued else {},
        placement="queued" if queued else "placed",
    )


def test_notification_during_idle_exit_dispatches_without_another_notification(tmp_path: Path, monkeypatch):
    session = DurableSession(SQLiteDurableStore(tmp_path / "durable.sqlite3"))
    _admit(session, "first")
    dispatched = threading.Event()
    retiring = threading.Event()
    release = threading.Event()
    finish_dispatch = threading.Event()
    retiring_threads = []
    dispatch_threads = []

    def dispatch(task):
        if task.task_id == "late":
            dispatch_threads.append(threading.current_thread())
            dispatched.set()
            assert finish_dispatch.wait(5), "successor dispatch barrier was not released"
        return True

    scheduler = DurableTaskScheduler(session, dispatch)
    run = scheduler._run
    run_code = run.__func__.__code__
    source, first_line = inspect.getsourcelines(run)
    # Pause after the idle decision, before final cleanup can release or
    # overwrite the thread handle. Both the old and fixed code pass here.
    cleanup_line = first_line + next(i for i, line in enumerate(source) if line.strip() == "finally:") + 1

    def trace(frame, event, arg):
        if frame.f_code is not run_code:
            return None
        if event == "line" and frame.f_lineno == cleanup_line and not retiring.is_set():
            retiring_threads.append(threading.current_thread())
            retiring.set()
            assert release.wait(5), "idle-exit barrier was not released"
        return trace

    def traced_run():
        previous_trace = sys.gettrace()
        sys.settrace(trace)
        try:
            run()
        finally:
            sys.settrace(previous_trace)

    monkeypatch.setattr(scheduler, "_run", traced_run)
    try:
        scheduler.start()
        assert retiring.wait(5)
        _admit(session, "late")
        scheduler.notify("late")
        release.set()
        retiring_threads[0].join(5)
        assert not retiring_threads[0].is_alive()
        assert dispatched.wait(2), "notification was stranded by the retiring scheduler"
        assert scheduler._thread is dispatch_threads[0], "retiring thread cleared its successor's handle"
        assert session.task("late").status == TaskStatus.RUNNING
    finally:
        release.set()
        finish_dispatch.set()
        assert scheduler.close()
        session.close()


def test_completion_during_queue_scan_places_input_without_another_notification(tmp_path: Path, monkeypatch):
    session = DurableSession(SQLiteDurableStore(tmp_path / "durable.sqlite3"))
    _admit(session, "first")
    session.claim_task("first")
    session.start_task("first", state={})
    _admit(session, "queued", queued=True)
    scanning = threading.Event()
    release = threading.Event()
    dispatched = threading.Event()
    scheduler = DurableTaskScheduler(session, lambda task: dispatched.set() or True)
    remove = scheduler._remove

    def pause_before_removing_queued(task_id):
        if task_id == "queued" and not scanning.is_set():
            scanning.set()
            assert release.wait(5), "queue-scan barrier was not released"
        remove(task_id)

    monkeypatch.setattr(scheduler, "_remove", pause_before_removing_queued)
    try:
        scheduler.start()
        assert scanning.wait(5)
        session.complete_task("first", "finished", state={})
        # DurableAppServices sends this notification for a terminal commit.
        scheduler.notify()
        release.set()
        assert dispatched.wait(2), "queue reconciliation was lost while the scheduler became idle"
        assert session.task("first").status == TaskStatus.COMPLETED
        assert session.task("queued").state["input_state"] == "placed"
        assert session.task("queued").status == TaskStatus.RUNNING
    finally:
        release.set()
        assert scheduler.close()
        session.close()


def test_requeue_after_dispatch_snapshot_is_not_lost(tmp_path, monkeypatch):
    session = DurableSession(SQLiteDurableStore(tmp_path / "durable.sqlite3"))
    _admit(session, "resume")
    calls = []
    dispatch_returned = False
    interleaved = False

    def dispatch(task):
        nonlocal dispatch_returned
        calls.append(task.attempt_token)
        session.start_task(task.task_id, state={})
        dispatch_returned = True
        return True

    scheduler = DurableTaskScheduler(session, dispatch)
    read = session.task

    def stale_dispatch_snapshot(task_id):
        nonlocal interleaved
        current = read(task_id)
        if dispatch_returned and not interleaved:
            interleaved = True
            # The worker parks and an approval resumes the same task after
            # the dispatcher read RUNNING, before it updates volatile sets.
            session.wait_task(task_id, state={"wait_reason": "approval"})
            session.advance_task(task_id=task_id, phase="provider_prepared", state={"next": "resume"})
            session.unclaim_task(task_id)
            scheduler.notify(task_id, start=False)
        return current

    monkeypatch.setattr(session, "task", stale_dispatch_snapshot)
    try:
        scheduler._reconcile()
        assert interleaved and read("resume").status == TaskStatus.PENDING
        scheduler._reconcile()
        assert len(calls) == 2, "the committed continuation was suppressed by a stale dispatch snapshot"
        assert calls[0] != calls[1], "resumption must claim a new attempt"
    finally:
        scheduler.close()
        session.close()
