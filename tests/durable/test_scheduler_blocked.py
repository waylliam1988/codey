from __future__ import annotations

from codey.durable import DurableSession, SQLiteDurableStore, TaskStatus
from codey.durable.scheduler import DurableTaskScheduler
from codey.durable.workflow import WorkflowDefinition


def test_scheduler_exposes_missing_workflow_as_blocked_and_reconsiders_registry(tmp_path) -> None:
    session = DurableSession(SQLiteDurableStore(tmp_path / "durable.sqlite3"))
    session.admit_input(
        conversation_id="conversation",
        request_id="request",
        task_id="task",
        text="workflow",
        provider_snapshot={"provider_id": "local"},
        workflow_name="missing",
        workflow_version=1,
        workflow_checkpoint={},
    )
    dispatched: list[str] = []
    scheduler = DurableTaskScheduler(session, lambda task: dispatched.append(task.task_id) or True)

    scheduler._reconcile()

    assert session.task("task").status is TaskStatus.PENDING
    assert scheduler.blocked_tasks == {"task": "missing_task"}
    assert dispatched == []

    scheduler.set_workflows({"missing": WorkflowDefinition(name="missing", version=1)})
    scheduler._reconcile()

    assert scheduler.blocked_tasks == {}
    assert dispatched == ["task"]
