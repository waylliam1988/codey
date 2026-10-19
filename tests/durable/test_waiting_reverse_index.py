from __future__ import annotations

from codey.durable import DurableSession, MemoryDurableStore, SQLiteDurableStore, TaskStatus
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


def test_waiting_reverse_index_is_durable_and_survives_reopen(tmp_path) -> None:
    path = tmp_path / "durable.sqlite3"
    session = DurableSession(SQLiteDurableStore(path))
    for task_id in ("child", "waiter-a", "waiter-b"):
        session.admit_input(
            conversation_id="conversation",
            request_id=f"request-{task_id}",
            task_id=task_id,
            text=task_id,
            provider_snapshot={"provider_id": "local"},
        )
    for waiter in ("waiter-a", "waiter-b"):
        session.claim_task(waiter)
        session.start_task(waiter, state={})
        session.wait_task(waiter, on=("child",))

    assert [task.task_id for task in session.store.waiting_dependents("child")] == [
        "waiter-a", "waiter-b",
    ]
    session.close()

    reopened = DurableSession(SQLiteDurableStore(path))
    try:
        assert [task.task_id for task in reopened.store.waiting_dependents("child")] == [
            "waiter-a", "waiter-b",
        ]
    finally:
        reopened.close()


def test_memory_waiting_reverse_index_updates_with_checkpoint() -> None:
    session = DurableSession(MemoryDurableStore())
    for task_id in ("child", "waiter"):
        session.admit_input(
            conversation_id="conversation",
            request_id=f"request-{task_id}",
            task_id=task_id,
            text=task_id,
            provider_snapshot={"provider_id": "local"},
        )
    session.claim_task("waiter")
    session.start_task("waiter", state={})
    session.wait_task("waiter", on=("child",))
    assert [task.task_id for task in session.store.waiting_dependents("child")] == ["waiter"]
    session.complete_task("child", "done", state={})
    session.reconcile_waiting_tasks()
    assert session.store.waiting_dependents("child") == ()
    session.close()
