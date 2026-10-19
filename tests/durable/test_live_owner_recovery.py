from __future__ import annotations

from codey.durable import DurableSession, MemoryDurableStore, TaskStatus


def test_recovery_does_not_reclaim_a_live_attempt_owner() -> None:
    store = MemoryDurableStore()
    owner = DurableSession(store)
    observer = DurableSession(store)
    owner.admit_input(
        conversation_id="conversation",
        request_id="request",
        task_id="task",
        text="work",
        provider_snapshot={"provider_id": "local"},
    )
    owner.claim_task("task")
    before = owner.task("task")

    observer.recover_scheduler_state()

    after = owner.task("task")
    assert after.status is TaskStatus.RUNNING
    assert after.attempt_token == before.attempt_token
    assert after.state["attempt_process_id"] == before.state["attempt_process_id"]
    owner.close()
    observer.close()
