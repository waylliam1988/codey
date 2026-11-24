import threading
from unittest.mock import patch

import pytest

from codey.durable import DurableSession, MemoryDurableStore, TaskCheckpoint
from codey.durable.scheduler import AbortInvocation, DurableTaskScheduler


def test_failed_abort_thread_start_leaves_cancellation_retryable() -> None:
    session = DurableSession(MemoryDurableStore())
    session.admit_input(
        conversation_id="conversation", request_id="request", task_id="task",
        text="work", provider_snapshot={},
    )
    dispatched = threading.Event()
    finish_dispatch = threading.Event()
    cleaned = threading.Event()

    def dispatch(_task: TaskCheckpoint) -> bool:
        dispatched.set()
        assert finish_dispatch.wait(5)
        return True

    def cleanup(task: TaskCheckpoint, _invocation: AbortInvocation) -> None:
        assert task.state["cancel_requested"] is True
        cleaned.set()

    scheduler = DurableTaskScheduler(session, dispatch, abort_handler=cleanup, retry_interval=0.01)
    scheduler.start()
    try:
        assert dispatched.wait(5)
        with (
            patch("threading.Thread.start", side_effect=RuntimeError("thread unavailable")),
            pytest.raises(RuntimeError, match="thread unavailable"),
        ):
            scheduler.abort_task("task")

        assert session.task("task").state["cancel_requested"] is True
        assert scheduler.abort_invocation("task") is None
        assert not cleaned.is_set()

        scheduler.abort_task("task")
        assert cleaned.is_set()
    finally:
        finish_dispatch.set()
        scheduler.close(timeout=0.25)
        session.close()
