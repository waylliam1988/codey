"""Terminal settlement: cancelled, error, and mode-outcome finish.
"""

from __future__ import annotations

from collections.abc import Callable
from contextlib import suppress
from dataclasses import replace
from typing import Any

from codey.operations.context import RunFrame, RunWork
from codey.operations.ghost_post_turn import (
    GhostTaskPolicyDeps,
    complete_or_block_work_item,
    maybe_sync_work_queue,
    release_work_item,
    run_ghost_post_turn,
)
from codey.operations.result import ModeOutcome
from codey.operations.task_phases.lifecycle import (
    _update_checkpoint_safely,
    finish_run_operation,
)
from codey.operations.task_state import TaskState
from codey.providers import PROVIDER_LABELS
from codey.providers.diagnostics import ProviderActionError
from codey.runs.ledger import RunLedgerWriter
from codey.runs.ledger_projection import event_with_projected_receipt
from codey.runtime.core.outcome import OperationOutcome
from codey.runtime.observe.terminalizer import (
    operation_outcome_from_task_done_event,
    task_done_event,
)
from codey.task.kind import ui_mode


def settle_cancelled_run(
    deps: Any,
    state: TaskState,
    work: RunWork | None,
    ghost_deps: GhostTaskPolicyDeps,
    *,
    run_id: str,
    session_id: str,
    max_turns: int,
    provider_id: str,
    task_kind: str,
    project: str | None,
    frame: RunFrame | None,
    finish_trace: Callable[[dict[str, object]], None],
    conversation: Any | None = None,
) -> OperationOutcome:
    """Settle a TaskCancelled stop; mirrors the previous inline except block."""
    state.set_provider_session(provider_id, None)
    if work is not None:
        _update_checkpoint_safely(deps, work, "stopped")
    if conversation is not None:
        with suppress(Exception):
            conversation.update_snapshot(
                replace(conversation.snapshot, provider_id=provider_id, blocker="stopped")
            )
    stopped_event = task_done_event(
        run_id=run_id,
        session_id=session_id,
        summary="",
        stop_reason="stopped",
        max_turns=max_turns,
        provider=provider_id,
        mode=ui_mode(task_kind, project),
        work=work,
    )
    if work is not None and work.ledger is not None:
        try:
            work.ledger.finish(**stopped_event)
        except Exception:
            work.ledger = None
    stopped_event = event_with_projected_receipt(
        deps.run_ledgers, stopped_event, session_id=session_id, run_id=run_id
    )
    if work is not None:
        finish_run_operation(deps, work, stopped_event)
    finish_trace(stopped_event)
    state.finish_run(run_id, stopped_event)
    release_work_item(
        ghost_deps,
        work.claimed_work_item if work is not None else None,
        run_id=run_id,
        reason="stopped",
    )
    return operation_outcome_from_task_done_event(stopped_event)


def settle_error_run(
    deps: Any,
    state: TaskState,
    exc: Exception,
    work: RunWork | None,
    ghost_deps: GhostTaskPolicyDeps,
    *,
    run_id: str,
    session_id: str,
    max_turns: int,
    provider_id: str,
    task_kind: str,
    project: str | None,
    frame: RunFrame | None,
    provider: Any | None,
    finish_trace: Callable[[dict[str, object]], None],
    conversation: Any | None = None,
) -> OperationOutcome:
    """Settle an unexpected error; mirrors the previous inline except block."""
    from codey.runtime.effects.tool_result_delivery import ToolResultDeliveryError

    if work is not None:
        _update_checkpoint_safely(deps, work, "error")
    if conversation is not None:
        with suppress(Exception):
            conversation.update_snapshot(
                replace(conversation.snapshot, provider_id=provider_id, blocker=str(exc))
            )
    failure = None
    if not isinstance(exc, ToolResultDeliveryError):
        failure = (
            exc.failure
            if isinstance(exc, ProviderActionError)
            else deps.capture_provider_failure(
                model=PROVIDER_LABELS.get(provider_id, provider_id),
                action="task" if provider is not None else "connect",
                page=None,
                error=exc,
            )
        )
    state.run_registry.set_last_provider_failure(failure)
    if failure is not None and work is not None and work.ledger is not None:
        try:
            work.ledger.append_provider_failure(provider_id, failure)
        except Exception:
            assert work is not None
            work.ledger = None
    error_event = task_done_event(
        run_id=run_id,
        session_id=session_id,
        summary=f"ERROR: {exc}",
        stop_reason="error",
        max_turns=max_turns,
        provider=provider_id,
        mode=ui_mode(task_kind, project),
        work=work,
        provider_failure=failure.to_dict() if failure else None,
    )
    if work is not None and work.ledger is not None:
        try:
            work.ledger.finish(**error_event)
        except Exception:
            work.ledger = None
    error_event = event_with_projected_receipt(
        deps.run_ledgers, error_event, session_id=session_id, run_id=run_id
    )
    if work is not None:
        finish_run_operation(deps, work, error_event)
    finish_trace(error_event)
    state.finish_run(run_id, error_event)
    current_work_item = work.claimed_work_item if work is not None else None
    if frame is not None:
        complete_or_block_work_item(ghost_deps, frame, error_event, current_work_item)
        maybe_sync_work_queue(ghost_deps, frame, error_event)
    else:
        run_ghost_post_turn(
            ghost_deps, None, error_event, current_work_item, project_text=str(project or "")
        )
    return operation_outcome_from_task_done_event(error_event)


def finish_mode_outcome(
    deps: Any,
    ghost_deps: GhostTaskPolicyDeps,
    frame: RunFrame,
    work: RunWork,
    outcome: ModeOutcome,
    *,
    append_ledger: Callable[[Callable[[RunLedgerWriter], None]], None],
    finish_trace: Callable[[dict[str, object]], None],
) -> OperationOutcome:
    """Public orchestrator for mode finish; delegates to the durable helper."""
    return _finish_mode_outcome(
        deps, ghost_deps, frame, work, outcome,
        append_ledger=append_ledger, finish_trace=finish_trace,
    )


def _finish_mode_outcome(
    deps: Any,
    ghost_deps: GhostTaskPolicyDeps,
    frame: RunFrame,
    work: RunWork,
    outcome: ModeOutcome,
    *,
    append_ledger: Callable[[Callable[[RunLedgerWriter], None]], None],
    finish_trace: Callable[[dict[str, object]], None],
) -> OperationOutcome:
    append_ledger(lambda ledger: ledger.finish(**outcome.event))
    event = event_with_projected_receipt(
        deps.run_ledgers,
        outcome.event,
        session_id=frame.request.session_id,
        run_id=frame.run_id,
    )
    finish_run_operation(deps, work, event)
    finish_trace(event)
    deps.state.finish_run(frame.run_id, event)
    run_ghost_post_turn(
        ghost_deps,
        frame,
        event,
        work.claimed_work_item,
        research_result=outcome.research_result,
    )
    return operation_outcome_from_task_done_event(event)
