from __future__ import annotations

import threading
import time

from codey.durable import DurableSession, MemoryDurableStore, TaskStatus
from codey.durable.scheduler import AbortInvocation, DurableTaskScheduler


def test_abort_runs_independent_handler_after_durable_mark_and_signal() -> None:
    session = DurableSession(MemoryDurableStore())
    session.admit_input(
        conversation_id="conversation",
        request_id="request",
        task_id="task",
        text="work",
        provider_snapshot={"provider_id": "local"},
    )
    started = threading.Event()
    handler_seen = threading.Event()
    observed: list[object] = []
    scheduler: DurableTaskScheduler

    def dispatch(task) -> bool:
        started.set()
        invocation = scheduler.invocation(task.task_id)
        assert invocation is not None
        assert invocation.signal.wait(2)
        observed.append(("signal", session.task(task.task_id).state.get("cancel_requested")))
        return True

    def abort_handler(task, invocation: AbortInvocation) -> None:
        observed.append(("handler", task.state.get("cancel_requested"), invocation.task_id))
        handler_seen.set()

    scheduler = DurableTaskScheduler(
        session, dispatch, retry_interval=0.01, abort_handler=abort_handler,
    )
    scheduler.start()
    try:
        assert started.wait(2)
        scheduler.abort_task("task", reason="user")
        assert handler_seen.is_set()
        assert observed[0] == ("signal", True)
        assert observed[1] == ("handler", True, "task")
        assert _wait_until(lambda: session.task("task").status is TaskStatus.CANCELLED)
    finally:
        scheduler.close()
        session.close()


def _wait_until(predicate, timeout: float = 2.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return predicate()

