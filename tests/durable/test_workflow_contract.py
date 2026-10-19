from __future__ import annotations

from pathlib import Path

import pytest

from codey.durable import DurableSession, SQLiteDurableStore
from codey.durable.workflow import WorkflowDefinition, workflow_digest


def _admit(session: DurableSession, *, version: int, checkpoint: dict[str, object]) -> None:
    session.admit_input(
        conversation_id="conversation-1",
        request_id=f"request-v{version}",
        task_id=f"task-v{version}",
        text="run workflow",
        provider_snapshot={"provider_id": "test"},
        workflow_name="counter",
        workflow_version=version,
        workflow_input={"seed": 1, "labels": ["a", "b"]},
        workflow_checkpoint=checkpoint,
    )


def test_workflow_definition_is_frozen_in_checkpoint_and_digest_is_canonical(tmp_path: Path) -> None:
    session = DurableSession(SQLiteDurableStore(tmp_path / "durable.sqlite3"))
    try:
        _admit(session, version=1, checkpoint={"counter": 1, "nested": {"b": 2, "a": 1}})
        task = session.task("task-v1")
        assert task.workflow_name == "counter"
        assert task.workflow_version == 1
        assert task.replay_digest == workflow_digest(
            "counter", 1, {"seed": 1, "labels": ["a", "b"]}, {"counter": 1, "nested": {"a": 1, "b": 2}}
        )
        session.advance_task(task_id="task-v1", phase="dispatching", state={"workflow_checkpoint": {"counter": 2}})
        assert session.task("task-v1").replay_digest == workflow_digest(
            "counter", 1, {"seed": 1, "labels": ["a", "b"]}, {"counter": 2}
        )
    finally:
        session.close()


def test_workflow_migration_is_explicit_and_durable(tmp_path: Path) -> None:
    session = DurableSession(SQLiteDurableStore(tmp_path / "durable.sqlite3"))
    try:
        _admit(session, version=1, checkpoint={"counter": 1})
        definition = WorkflowDefinition(
            name="counter",
            version=2,
            migrate=lambda checkpoint: {**checkpoint, "counter2": int(checkpoint["counter"]) * 2},
        )
        changed = session.reconcile_workflows({"counter": definition})
        assert changed == ("task-v1",)
        task = session.task("task-v1")
        assert task.workflow_version == 2
        assert task.state["workflow_checkpoint"]["counter2"] == 2
        assert task.state["workflow_blocked_reason"] is None
    finally:
        session.close()


def test_newer_stored_workflow_is_blocked_without_mutation(tmp_path: Path) -> None:
    session = DurableSession(SQLiteDurableStore(tmp_path / "durable.sqlite3"))
    try:
        _admit(session, version=2, checkpoint={"counter": 1})
        before = session.task("task-v2")
        changed = session.reconcile_workflows({"counter": WorkflowDefinition(name="counter", version=1)})
        assert changed == ()
        after = session.task("task-v2")
        assert after.revision == before.revision
        assert after.state.get("workflow_blocked_reason") is None
    finally:
        session.close()


def test_workflow_migration_requires_a_migrator() -> None:
    with pytest.raises(ValueError, match="migration"):
        WorkflowDefinition(name="counter", version=2).migrate_checkpoint({"counter": 1}, 1)

