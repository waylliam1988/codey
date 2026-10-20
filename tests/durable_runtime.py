from __future__ import annotations

from pathlib import Path

from codey.durable import DurableSession, SQLiteDurableStore
from codey.runtime.write.mutation_line import RuntimeMutationLine


def open_runtime(
    root: Path,
    *,
    session_id: str,
    run_id: str,
    task_kind: str = "project",
    provider_id: str = "local",
    phase: str = "assistant_response_pending",
) -> tuple[DurableSession, RuntimeMutationLine]:
    durable = DurableSession(SQLiteDurableStore(root / "durable.sqlite3"))
    durable.admit_input(
        conversation_id=session_id,
        request_id=f"request-{run_id}",
        task_id=run_id,
        text="runtime test",
        provider_snapshot={"provider_id": provider_id, "model_id": "test"},
        task_state={"task_kind": task_kind},
    )
    durable.start_task(run_id, state={"task_kind": task_kind})
    if phase != "provider_prepared":
        durable.advance_task(task_id=run_id, phase="assistant_intent_committed", state={"task_kind": task_kind})
        durable.advance_task(task_id=run_id, phase=phase, state={"task_kind": task_kind})
    return durable, RuntimeMutationLine(durable=durable)


def accept_writer(durable: DurableSession, line: RuntimeMutationLine, *, session_id: str, run_id: str,
                  project: str = "", provider_id: str = "local") -> None:
    line.accept_operation(
        session_id=session_id, run_id=run_id, project=project,
        provider_id=provider_id, turn_budget=10, max_repair_rounds=1, task_kind="project",
    )
    line.mark_writer_running(session_id, run_id, provider_id=provider_id)
