from __future__ import annotations

import sys
import threading
import time
import traceback

from codey.durable import DurableSession, SQLiteDurableStore, TaskStatus
from codey.durable.scheduler import DurableTaskScheduler


def test_abort_marks_task_then_signals_active_dispatch_invocation(tmp_path) -> None:
    session = DurableSession(SQLiteDurableStore(tmp_path / "durable.sqlite3"))
    session.admit_input(
        conversation_id="conversation",
        request_id="request",
        task_id="task",
        text="work",
        provider_snapshot={"provider_id": "local"},
    )
    started = threading.Event()
    released = threading.Event()
    scheduler: DurableTaskScheduler

    def dispatch(task) -> bool:
        started.set()
        invocation = scheduler.invocation(task.task_id)
        assert invocation is not None
        assert invocation.attempt_token == task.attempt_token
        assert invocation.mode == "run"
        assert invocation.signal.wait(10)
        released.set()
        return True

    scheduler = DurableTaskScheduler(session, dispatch, retry_interval=0.01)
    scheduler.start()
    try:
        _wait_for_dispatch(started, scheduler)
        scheduler.abort_task("task", reason="user")
        assert session.task("task").state["cancel_requested"] is True
        assert released.wait(10)
        assert _wait_until(lambda: session.task("task").status is TaskStatus.CANCELLED, timeout=10)
    finally:
        scheduler.close()
        session.close()


def test_close_signals_active_invocation_and_joins_dispatch(tmp_path) -> None:
    session = DurableSession(SQLiteDurableStore(tmp_path / "durable.sqlite3"))
    session.admit_input(
        conversation_id="conversation",
        request_id="request-close",
        task_id="task-close",
        text="work",
        provider_snapshot={"provider_id": "local"},
    )
    started = threading.Event()
    released = threading.Event()
    scheduler: DurableTaskScheduler

    def dispatch(task) -> bool:
        started.set()
        invocation = scheduler.invocation(task.task_id)
        assert invocation is not None
        assert invocation.signal.wait(2)
        released.set()
        return True

    scheduler = DurableTaskScheduler(session, dispatch, retry_interval=0.01)
    scheduler.start()
    try:
        _wait_for_dispatch(started, scheduler)
        assert scheduler.close(timeout=1)
        assert released.is_set()
    finally:
        scheduler.close()
        session.close()


def _wait_for_dispatch(started: threading.Event, scheduler: DurableTaskScheduler, timeout: float = 10.0) -> None:
    # Real SQLite commits can outlast two seconds. This is a readiness barrier,
    # not a latency benchmark; cancellation and join deadlines remain separate.
    if started.wait(timeout):
        return
    thread = scheduler._thread
    frame = sys._current_frames().get(thread.ident) if thread and thread.ident is not None else None
    stack = "".join(traceback.format_stack(frame)) if frame is not None else "No live scheduler frame."
    # Do not query SQLite here: diagnostics must not wait on the stalled I/O.
    raise AssertionError(
        f"Scheduler did not dispatch within {timeout}s; "
        f"thread={thread.name if thread else None}, alive={bool(thread and thread.is_alive())}\n{stack}"
    )


def _wait_until(predicate, timeout: float = 2.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return predicate()
