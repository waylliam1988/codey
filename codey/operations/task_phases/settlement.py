"""Terminal settlement: cancelled, error, and mode-outcome finish.
"""

from __future__ import annotations

from collections.abc import Callable
from contextlib import suppress
from dataclasses import replace
from typing import TYPE_CHECKING

from codey.agents.handoff import ConversationContext
from codey.providers.base import ChatProvider
from codey.providers.transport import ProviderTransport

if TYPE_CHECKING:
    from codey.operations.task_run import TaskRunDeps

from codey.durable.errors import DurableStoreError
from codey.durable.events import committed_task_version
from codey.operations.context import RunFrame, RunWork
from codey.operations.result import ModeOutcome
from codey.operations.task_phases.lifecycle import (
    _update_checkpoint_safely,
    finish_run_operation,
)
from codey.operations.task_state import TaskState
from codey.providers.catalog import PROVIDER_LABELS
from codey.providers.diagnostics import ProviderActionError
from codey.runs.ledger import RunLedgerWriter
from codey.runs.ledger_projection import event_with_projected_receipt
from codey.runtime.core.operation_payload import RuntimeOperationTransitionError
from codey.runtime.core.outcome import OperationOutcome
from codey.runtime.observe.terminalizer import (
    operation_outcome_from_task_done_event,
    task_done_event,
)
from codey.task.kind import ui_mode


def _display_reply_text(outcome: ModeOutcome) -> str:
    """True user-visible reply carried by the mode, if any.

    Review publishes its reply through ``display``; workspace tasks surface
    the task summary itself. The observation must record what the user
    actually saw, not the internal settlement summary.
    """
    for payload in tuple(getattr(outcome, "display", ()) or ()):
        if not isinstance(payload, dict):
            continue
        text = str(payload.get("text") or "").strip()
        if text:
            return text
    return ""








def _publish_display_after_commit(deps: TaskRunDeps, outcome: ModeOutcome) -> None:
    """Emit final display events carried by the mode (settlement-owned)."""
    for payload in tuple(getattr(outcome, "display", ()) or ()):
        if not isinstance(payload, dict) or not payload.get("type"):
            continue
        try:
            deps.state.emit(dict(payload))
        except Exception:
            continue


def _commit_durable_terminal(deps: TaskRunDeps, frame: RunFrame, event: dict[str, object], outcome: ModeOutcome) -> None:
    durable = getattr(deps.state, "durable", None)
    if durable is None:
        return
    run_id = str(event.get("run_id") or frame.run_id)
    try:
        task = durable.task(run_id)
    except KeyError:
        return
    # A headless/user stop may have already won the canonical cancellation
    # race.  The runtime operation still records the provider's terminal
    # outcome, but it must not try to park or complete an already-cancelled
    # durable task.
    if task.phase in {"failed", "cancelled", "completed"}:
        event.update(task_revision=task.revision, task_status=str(task.status))
        return
    state: dict[str, object] = {
        "stop_reason": str(event.get("stop_reason") or ""),
        "summary": str(event.get("summary") or ""),
        "provider": str(event.get("provider") or ""),
        "turns": event.get("turns") if isinstance(event.get("turns"), int) else 0,
    }
    receipt = event.get("receipt")
    if isinstance(receipt, dict):
        proof = receipt.get("completion_proof")
        if isinstance(proof, dict):
            state["completion_proof"] = {
                "proof_id": str(proof.get("proof_id") or ""),
                "status": str(proof.get("status") or ""),
            }
    terminal = operation_outcome_from_task_done_event(event)
    if terminal.status == "completed":
        answer = _display_reply_text(outcome) or str(event.get("summary") or "")
        committed = durable.complete_task(run_id, answer, state=state)
        event.update(committed_task_version(committed, run_id))
    elif terminal.status == "suspended":
        # The provider turn yielded to an external decision.  Preserve the
        # exact logical checkpoint and park scheduler ownership; the decision
        # worker or answer admission owns the explicit wake-up/continuation.
        committed = durable.wait_task(run_id, state=state)
        event.update(committed_task_version(committed, run_id))
    elif task.phase not in {"failed", "cancelled", "completed"}:
        committed = durable.fail_task(run_id, str(event.get("summary") or event.get("stop_reason") or "task failed"), state=state)
        event.update(committed_task_version(committed, run_id))


def _fail_durable_task(deps: TaskRunDeps, run_id: str, event: dict[str, object]) -> None:
    durable = getattr(deps.state, "durable", None)
    if durable is None:
        return
    try:
        task = durable.task(run_id)
    except KeyError:
        return
    if task.phase in {"failed", "cancelled", "completed"}:
        return
    state = {
        "stop_reason": str(event.get("stop_reason") or ""),
        "summary": str(event.get("summary") or ""),
        "provider": str(event.get("provider") or ""),
    }
    if task.state.get("cancel_requested"):
        durable.cancel_task(
            run_id,
            reason=str(task.state.get("cancel_reason") or "user stopped"),
        )
    else:
        durable.fail_task(
            run_id,
            str(event.get("summary") or event.get("stop_reason") or "task failed"),
            state=state,
        )


def _seal_cancel_before_runtime(deps: TaskRunDeps, run_id: str, event: dict[str, object]) -> bool:
    """Close a durable cancellation barrier before runtime projection writes."""
    durable = getattr(getattr(deps, "state", None), "durable", None)
    if durable is None:
        return False
    try:
        task = durable.task(run_id)
    except KeyError:
        return False
    if not task.state.get("cancel_requested"):
        return False
    _settle_research_publications(deps, task.conversation_id, run_id)
    _fail_durable_task(deps, run_id, event)
    return True


def _settle_research_publications(deps: TaskRunDeps, session_id: str, run_id: str) -> None:
    from codey.operations.research_continuation import ResearchContinuation
    from codey.operations.research_publication import settle_cancelled_publications
    from codey.runtime.write.checkpoint import operation_from_checkpoint
    from codey.runtime.write.research_publication import PUBLICATION_TOOL

    mutations = getattr(deps, "runtime_mutations", None)
    if mutations is None:
        return
    try:
        position = operation_from_checkpoint(mutations.durable.task(run_id), session_id)
    except KeyError:
        return
    if position is None or position.task_kind != "research":
        return
    if not any(effect.intent.get("tool_name") == PUBLICATION_TOOL and effect.settlement is None
               for effect in mutations.durable.effects(run_id)):
        return
    history = ResearchContinuation(mutations, deps.managed_outputs, durable=deps.state.durable,
                                   session_id=session_id, run_id=run_id)
    settle_cancelled_publications(history, getattr(deps, "knowledge_store", None))


def _finish_failed_operation(deps: TaskRunDeps, work: RunWork | None, event: dict[str, object]) -> None:
    """Report an uncommittable stop while retaining its authoritative position."""
    if work is None:
        return
    try:
        finish_run_operation(deps, work, event)
    except (OSError, ValueError, DurableStoreError, RuntimeOperationTransitionError) as exc:
        event["runtime_settlement_error"] = {"source": "checkpoint_commit", "type": type(exc).__name__,
                                             "message": str(exc)}


def settle_cancelled_run(
    deps: TaskRunDeps,
    state: TaskState,
    work: RunWork | None,
    *,
    run_id: str,
    session_id: str,
    max_turns: int,
    provider_id: str,
    task_kind: str,
    project: str | None,
    frame: RunFrame | None,
    finish_trace: Callable[[dict[str, object]], None],
    conversation: ConversationContext | None = None,
) -> OperationOutcome:
    """Settle a TaskCancelled stop; mirrors the previous inline except block."""
    _settle_research_publications(deps, session_id, run_id)
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
    cancelled = _seal_cancel_before_runtime(deps, run_id, stopped_event)
    _finish_failed_operation(deps, work, stopped_event)
    # A provider can observe the stop barrier without a separate durable
    # ``request_cancel`` transition (for example headless cancellation during
    # a native send).  The terminal event still owns the task outcome, so
    # close the canonical checkpoint here instead of leaving it runnable at
    # ``assistant_response_pending``.
    if not cancelled:
        _fail_durable_task(deps, run_id, stopped_event)
    finish_trace(stopped_event)
    state.finish_run(run_id, stopped_event)
    return operation_outcome_from_task_done_event(stopped_event)


def settle_error_run(
    deps: TaskRunDeps,
    state: TaskState,
    exc: Exception,
    work: RunWork | None,
    *,
    run_id: str,
    session_id: str,
    max_turns: int,
    provider_id: str,
    task_kind: str,
    project: str | None,
    frame: RunFrame | None,
    provider: ChatProvider | ProviderTransport | None,
    finish_trace: Callable[[dict[str, object]], None],
    conversation: ConversationContext | None = None,
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
    if not isinstance(exc, (ToolResultDeliveryError, RuntimeOperationTransitionError, DurableStoreError)):
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
    cancelled = _seal_cancel_before_runtime(deps, run_id, error_event)
    _finish_failed_operation(deps, work, error_event)
    finish_trace(error_event)
    if not cancelled:
        _fail_durable_task(deps, run_id, error_event)
    state.finish_run(run_id, error_event)
    return operation_outcome_from_task_done_event(error_event)


def finish_mode_outcome(
    deps: TaskRunDeps,
    frame: RunFrame,
    work: RunWork,
    outcome: ModeOutcome,
    *,
    append_ledger: Callable[[Callable[[RunLedgerWriter], None]], None],
    finish_trace: Callable[[dict[str, object]], None],
) -> OperationOutcome:
    """Public orchestrator for mode finish; delegates to the durable helper."""
    return _finish_mode_outcome(
        deps, frame, work, outcome,
        append_ledger=append_ledger, finish_trace=finish_trace,
    )


def _finish_mode_outcome(
    deps: TaskRunDeps,
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
    cancelled = _seal_cancel_before_runtime(deps, frame.run_id, event)
    if cancelled or event.get("stop_reason") not in {"done", "approval", "question"}:
        _finish_failed_operation(deps, work, event)
    else:
        finish_run_operation(deps, work, event)
    finish_trace(event)
    _commit_durable_terminal(deps, frame, event, outcome)
    _publish_display_after_commit(deps, outcome)
    deps.state.finish_run(frame.run_id, event)
    if event.get("stop_reason") == "approval":
        workflow = getattr(deps.state, "shell_approval_workflow", None)
        if workflow is not None:
            workflow.apply_remembered(frame.request.session_id, frame.run_id)
    return operation_outcome_from_task_done_event(event)
