from __future__ import annotations

from codey.durable import DurableSession, SQLiteDurableStore, TaskStatus
from codey.durable.scheduler import DurableTaskScheduler


def test_task_settlement_wakes_all_waiters_through_scheduler_notify(tmp_path) -> None:
    session = DurableSession(SQLiteDurableStore(tmp_path / "durable.sqlite3"))
    for task_id in ("child", "waiter-a", "waiter-b"):
        session.admit_input(
            conversation_id="conversation",
            request_id=f"request-{task_id}",
            task_id=task_id,
            text=task_id,
            provider_snapshot={"provider_id": "local"},
        )
    session.claim_task("waiter-a")
    session.start_task("waiter-a", state={})
    session.wait_task("waiter-a", on=("child",))
    session.claim_task("waiter-b")
    session.start_task("waiter-b", state={})
    session.wait_task("waiter-b", on=("child",))

    dispatched: list[str] = []

    def dispatch(task) -> bool:
        dispatched.append(task.task_id)
        session.start_task(task.task_id, state={})
        return True

    scheduler = DurableTaskScheduler(session, dispatch, retry_interval=0.01)
    session.claim_task("child")
    session.start_task("child", state={})
    try:
        session.complete_task("child", "done", state={})
        scheduler.notify("child", start=False)
        scheduler._reconcile()
        assert dispatched == ["waiter-a", "waiter-b"]
        assert session.task("waiter-a").status is TaskStatus.RUNNING
        assert session.task("waiter-b").status is TaskStatus.RUNNING
    finally:
        scheduler.close()
        session.close()
