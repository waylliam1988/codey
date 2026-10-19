from __future__ import annotations

import pytest

from codey.durable import DurableSession, MemoryDurableStore
from codey.durable.errors import CommitConflict
from codey.durable.model import EffectRecord


def _admitted(session: DurableSession) -> None:
    session.admit_input(
        conversation_id="conversation",
        request_id="request",
        task_id="task",
        text="run",
        provider_snapshot={"provider_id": "local"},
    )
    session.claim_task("task")


def test_old_worker_cannot_settle_after_restart_reclaims_attempt() -> None:
    store = MemoryDurableStore()
    old_worker = DurableSession(store)
    _admitted(old_worker)
    old_attempt = old_worker.task("task").attempt_token
    old_worker.start_task("task", state={})
    old_worker.advance_task(task_id="task", phase="assistant_response_pending", state={})
    old_worker.record_effect_intent(
        "task",
        EffectRecord(
            "effect", "task", "tool_call", "unsafe", "digest", "pending",
            {"session_id": "conversation", "run_id": "task", "tool_name": "edit"},
        ),
    )

    restarted = DurableSession(store)
    restarted.recover_scheduler_state()
    restarted.claim_task("task")
    assert restarted.task("task").attempt_token != old_attempt

    with pytest.raises(CommitConflict), old_worker.execution("task", old_attempt):
        old_worker.settle_unknown_effect("task", "effect")
