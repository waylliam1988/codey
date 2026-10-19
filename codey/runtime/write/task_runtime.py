"""Production task runtime entrypoint."""

from __future__ import annotations

import importlib
from dataclasses import dataclass, field
from typing import Any

from codey.runtime.core.operation import OperationContext, OperationIntent
from codey.runtime.core.operation_state import (
    LEAF_TERMINAL,
    lane_for_run,
    operation_id_for_run,
)
from codey.runtime.core.outcome import OperationOutcome
from codey.runtime.core.ports import TaskExecutor, TaskPreparer, TaskStartFailureHandler
from codey.runtime.write.mutation_line import RuntimeMutationLine
from codey.task.kind import resolve_task_kind
from codey.task.model import TaskSubmission


@dataclass
class _SubmittedTaskOperation:
    request: TaskSubmission
    executor: TaskExecutor
    operation_id: str
    kind: str
    lane: str
    intent: OperationIntent = field(default_factory=lambda: OperationIntent("task"))
    entered: bool = False

    def run(self, context: OperationContext) -> OperationOutcome | None:
        self.entered = True
        outcome = self.executor(self.request)
        if outcome is None:
            return OperationOutcome.failed(
                reason="missing_task_terminal",
                summary="task returned without terminal outcome",
            )
        return outcome


class TaskRuntime:
    """Schedule submitted tasks through durable storage."""

    def __init__(
        self,
        executor: TaskExecutor,
        *,
        prepare: TaskPreparer | None = None,
        on_unstarted_failure: TaskStartFailureHandler | None = None,
        durable: Any,
    ) -> None:
        self.executor = executor
        self.prepare = prepare
        self.on_unstarted_failure = on_unstarted_failure
        self.durable = durable
        if durable is None:
            raise ValueError("a task runtime requires durable storage")
        self.mutations = RuntimeMutationLine(durable=durable)

    def run(self, request: TaskSubmission) -> None:
        if self.prepare is not None:
            prepared = self.prepare(request)
            if prepared is None:
                return
            request = prepared
        if not request.run_id:
            raise ValueError("runtime task submissions require a run_id")
        run_id = request.run_id
        operation = _SubmittedTaskOperation(
            request=request,
            executor=self.executor,
            operation_id=operation_id_for_run(run_id),
            kind=resolve_task_kind(request),
            lane=lane_for_run(run_id),
            intent=OperationIntent(f"task:{run_id}"),
        )
        try:
            accepted = self.mutations.accept_operation(
                session_id=request.session_id,
                run_id=run_id,
                project=request.project or "",
                provider_id=request.provider_id,
                turn_budget=_turn_budget(request),
                max_repair_rounds=1,
                task_kind=resolve_task_kind(request),
                model_selection=request.model_selection or None,
            )
            if accepted is None:
                if self.on_unstarted_failure is not None:
                    self.on_unstarted_failure(request)
                return
            outcome = operation.run(
                OperationContext(
                    session_id=request.session_id,
                    run_id=run_id,
                    lane=operation.lane,
                )
            )
        except Exception:
            if not operation.entered and self.on_unstarted_failure is not None:
                self.on_unstarted_failure(request)
            self._settle_unhandled(request, operation.entered)
            raise
        if outcome is None:
            outcome = OperationOutcome.failed(
                reason="missing_task_terminal",
                summary="task returned without terminal outcome",
            )
        self._settle_if_open(request, outcome)

    def _settle_unhandled(self, request: TaskSubmission, entered: bool) -> None:
        if not entered:
            return
        self._settle_if_open(
            request,
            OperationOutcome.failed(reason="unhandled_task_exception"),
        )

    def _settle_if_open(
        self,
        request: TaskSubmission,
        outcome: OperationOutcome,
    ) -> None:
        if not request.run_id:
            return
        if outcome.status == "suspended":
            return
        # The inner task flow usually settles the operation first; this is a
        # backstop for executors that return without a terminal. Settle errors
        # propagate -- only an already-terminal operation is a clean no-op.
        if self.durable is not None:
            try:
                task = self.durable.task(request.run_id)
            except KeyError as exc:
                raise RuntimeError("durable task is missing for production submission") from exc
            if task.phase in {"completed", "failed", "cancelled"}:
                return
            checkpoint = importlib.import_module("codey.durable.checkpoint")
            operation = checkpoint.operation_from_checkpoint(task, request.session_id)
            if operation is not None and operation.leaf == LEAF_TERMINAL:
                return
        settled = self.mutations.mark_terminal(
            request.session_id,
            request.run_id,
            stop_reason=_stop_reason_for_outcome(outcome),
            summary_chars=len(outcome.summary),
            turns=0,
            max_turns=_turn_budget(request),
            provider=request.provider_id,
        )
        del settled


def _turn_budget(request: TaskSubmission) -> int:
    """Clamp the durable turn budget to the API contract (min 1)."""
    try:
        return max(1, (request.max_turns or 0))
    except (TypeError, ValueError):
        return 1


def _stop_reason_for_outcome(outcome: OperationOutcome) -> str:
    if outcome.status == "completed":
        return "done"
    if outcome.status == "aborted":
        return outcome.reason or "stopped"
    if outcome.status == "suspended":
        return outcome.reason or "approval"
    return outcome.reason or "error"

__all__ = ["TaskRuntime"]
