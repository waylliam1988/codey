"""Run-slot, workspace, ledger, and operation lifecycle helpers.
"""

from __future__ import annotations

from contextlib import suppress
from dataclasses import dataclass, replace
from typing import Any

from codey.operations.context import RunWork
from codey.operations.project_completion_flow import MAX_COMPLETION_REPAIR_ROUNDS
from codey.operations.task_state import TaskState
from codey.runtime.core.operation_state import RuntimeOperationTransitionError
from codey.runtime.core.outcome import OperationOutcome
from codey.runtime.observe.execution_evidence import ExecutionEvidence
from codey.runtime.observe.terminalizer import terminal_turns
from codey.task.kind import trace_mode, ui_mode
from codey.task.model import TaskSubmission
from codey.workspace.revision import INITIAL_WORKSPACE_REVISION, WorkspaceState


@dataclass(frozen=True)
class _Reservation:
    request: TaskSubmission
    run_id: str


def ensure_run_reserved_and_started(
    state: TaskState,
    request: TaskSubmission,
) -> tuple[_Reservation | None, OperationOutcome | None]:
    """Reserve the run slot and mark it started; pure orchestration, no mode logic."""
    run_id = request.run_id
    if not run_id:
        reserved = state.reserve_run(
            session_id=request.session_id,
            project=request.project,
            task=request.task,
            provider_id=request.provider_id,
        )
        if reserved is None:
            return None, None
        run_id = reserved.run_id
        request = replace(request, run_id=run_id)
    if not state.start_run(run_id):
        return None, OperationOutcome.aborted(reason="run_not_started")
    return _Reservation(request=request, run_id=run_id), None


def build_run_work(
    deps: Any,
    *,
    session_id: str,
    run_id: str,
    project: str | None,
    provider_id: str,
    max_turns: int,
    task_kind: str,
    ignored_paths: tuple[str, ...],
    trace: Any = None,
) -> tuple[RunWork | None, WorkspaceState]:
    """Build RunWork from the current workspace state and open the operation."""
    workspace_state = _current_workspace_state(deps, project, ignored_paths)
    work = RunWork(
        recent_events=[],
        evidence=ExecutionEvidence(
            workspace_revision=workspace_state.revision,
            workspace_fingerprint=workspace_state.fingerprint,
        ),
        claimed_work_item=None,
        trace=trace,
        workspace_revision=workspace_state.revision,
        workspace_fingerprint=workspace_state.fingerprint,
    )
    started_ok = start_run_operation(
        deps,
        work,
        session_id=session_id,
        run_id=run_id,
        project=project or "",
        provider_id=provider_id,
        turn_budget=max_turns,
        max_repair_rounds=MAX_COMPLETION_REPAIR_ROUNDS,
        task_kind=task_kind,
    )
    if not started_ok:
        return None, workspace_state
    return work, workspace_state


def open_run_trace(
    deps: Any,
    session_id: str,
    run_id: str,
    project: str | None,
    task_kind: str,
    provider_id: str,
):
    if deps.run_traces is None:
        return None
    try:
        return deps.run_traces.open(
            run_id=run_id,
            session_id=session_id,
            project=project,
            mode_initial=trace_mode(task_kind, project),
            provider_initial=provider_id,
        )
    except Exception:
        return None


def _current_workspace_state(
    deps: Any,
    project: str | None,
    ignored_paths: tuple[str, ...],
) -> WorkspaceState:
    store = deps.workspace_revisions
    if not project:
        return WorkspaceState(INITIAL_WORKSPACE_REVISION, "")
    return store.current_state(project, ignored_paths=ignored_paths)


def open_run_ledger(
    deps: Any,
    work: RunWork,
    request: TaskSubmission,
    *,
    run_id: str,
    task_kind: str,
    provider_id: str,
) -> None:
    if (
        request.project
        and task_kind in {"project", "hybrid", "planning_readonly", "review"}
        and deps.run_ledgers is not None
    ):
        try:
            work.ledger = deps.run_ledgers.open(
                run_id=run_id,
                session_id=request.session_id,
                project=request.project,
                task=request.task,
                provider=provider_id,
                mode=ui_mode(task_kind, request.project),
            )
            work.record_agent_events_in_ledger = task_kind in {"project", "planning_readonly"}
        except Exception:
            work.ledger = None


def start_run_operation(
    deps: Any,
    work: RunWork,
    *,
    session_id: str,
    run_id: str,
    project: str,
    provider_id: str,
    turn_budget: int,
    max_repair_rounds: int,
    task_kind: str,
) -> bool:
    try:
        mutations = getattr(deps, "runtime_mutations", None)
        if mutations is None:
            raise RuntimeOperationTransitionError("runtime mutation line is missing")
        work.operation = mutations.accept_operation(
            session_id=session_id,
            run_id=run_id,
            project=project,
            provider_id=provider_id,
            turn_budget=turn_budget,
            max_repair_rounds=max_repair_rounds,
            task_kind=task_kind,
        )
        return work.operation is not None
    except (OSError, ValueError, RuntimeOperationTransitionError):
        work.operation = None
        return False


def finish_run_operation(deps: Any, work: RunWork, event: dict[str, object]) -> None:
    if work.operation is None:
        return
    max_turns = int(event.get("max_turns") or 0)
    try:
        mutations = getattr(deps, "runtime_mutations", None)
        if mutations is None:
            raise RuntimeOperationTransitionError("runtime mutation line is missing")
        work.operation = mutations.mark_terminal(
            work.operation.session_id,
            work.operation.run_id,
            stop_reason=str(event.get("stop_reason") or ""),
            summary_chars=len(str(event.get("summary") or "")),
            turns=terminal_turns(work, turns=event.get("turns"), max_turns=max_turns),
            max_turns=max_turns,
            provider=str(event.get("provider") or ""),
        )
    except (OSError, ValueError, RuntimeOperationTransitionError):
        work.operation = None


def _update_checkpoint_safely(deps: Any, work: RunWork, reason: str) -> None:
    if deps.work_checkpoints is None or work.work_checkpoint is None:
        return
    with suppress(OSError, ValueError):
        work.work_checkpoint = deps.work_checkpoints.set_status(
            work.work_checkpoint,
            "interrupted",
            reason,
        )
