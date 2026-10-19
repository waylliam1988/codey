from __future__ import annotations

from codey.durable import DurableSession, MemoryDurableStore, SQLiteDurableStore, TaskStatus
from codey.durable.scheduler import DurableTaskScheduler


def test_scheduler_finalization_reconciles_only_durable_candidates(monkeypatch) -> None:
    store = MemoryDurableStore()
    session = DurableSession(store)
    session.admit_input(
        conversation_id="conversation", request_id="parent-request", task_id="parent",
        text="parent", provider_snapshot={},
    )
    session.admit_input(
        conversation_id="conversation", request_id="child-request", task_id="child",
        text="child", provider_snapshot={}, owner_task_id="parent",
    )
    session.complete_task("parent", "parent answer", state={})
    assert session.task("parent").status is TaskStatus.COMPLETING
    session.complete_task("child", "child answer", state={})

    def forbidden_tasks():
        raise AssertionError("local reconcile must not scan every task")

    monkeypatch.setattr(store, "tasks", forbidden_tasks)
    scheduler = DurableTaskScheduler(session, lambda _task: True)
    scheduler.notify("child", start=False)
    scheduler._reconcile()

    assert session.task("parent").status is TaskStatus.COMPLETED
    session.close()


def test_finalization_queue_is_durable_and_scheduler_does_not_derive_it_from_tasks(tmp_path) -> None:
    path = tmp_path / "durable.sqlite3"
    session = DurableSession(SQLiteDurableStore(path))
    session.admit_input(
        conversation_id="conversation", request_id="parent-request", task_id="parent",
        text="parent", provider_snapshot={},
    )
    session.admit_input(
        conversation_id="conversation", request_id="child-request", task_id="child",
        text="child", provider_snapshot={}, owner_task_id="parent",
    )
    session.complete_task("parent", "parent answer", state={})
    assert session.store.finalization_queue() == ("parent",)
    session.close()

    restarted = DurableSession(SQLiteDurableStore(path))
    try:
        assert restarted.store.finalization_queue() == ("parent",)

        def forbidden_tasks():
            raise AssertionError("finalization recovery must not scan every task")

        restarted.store.tasks = forbidden_tasks  # type: ignore[method-assign]
        scheduler = DurableTaskScheduler(restarted, lambda _task: True)
        scheduler._reconcile()
        assert restarted.store.finalization_queue() == ("parent",)
    finally:
        restarted.close()

