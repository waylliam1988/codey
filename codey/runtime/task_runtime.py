"""Production task runtime entrypoint."""

from __future__ import annotations

from dataclasses import dataclass, field

from codey.runtime.operation import OperationContext, OperationIntent
from codey.runtime.operation_state import lane_for_run, mark_terminal, operation_id_for_run
from codey.runtime.outcome import OperationOutcome
from codey.runtime.ports import TaskExecutor, TaskPreparer, TaskStartFailureHandler
from codey.runtime.mutation_line import RuntimeMutationLine
from codey.runtime.session_log import RuntimeSessionLog
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

    def run(self, context: OperationContext) -> OperationOutcome:
        self.entered = True
        outcome = self.executor(self.request)
        if outcome is None:
            return OperationOutcome.failed(
                reason="missing_task_terminal",
                summary="task returned without terminal outcome",
            )
        return outcome


class TaskRuntime:
    """Schedule submitted tasks through the runtime session log."""

    def __init__(
        self,
        session_log: RuntimeSessionLog,
        executor: TaskExecutor,
        *,
        prepare: TaskPreparer | None = None,
        on_unstarted_failure: TaskStartFailureHandler | None = None,
    ) -> None:
        self.session_log = session_log
        self.executor = executor
        self.prepare = prepare
        self.on_unstarted_failure = on_unstarted_failure
        self.mutations = RuntimeMutationLine(session_log)

    def run(self, request: TaskSubmission) -> None:
        if self.prepare is not None:
            request = self.prepare(request)
            if request is None:
                return
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
                turn_budget=max(0, int(request.max_turns or 0)),
                max_repair_rounds=1,
                task_kind=resolve_task_kind(request),
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
        try:
            current = self.mutations.transition_operation(
                request.session_id,
                request.run_id,
                lambda state: (
                    state
                    if state.leaf == "terminal"
                    else mark_terminal(
                        state,
                        stop_reason=_stop_reason_for_outcome(outcome),
                        summary_chars=len(outcome.summary),
                        turns=0,
                        max_turns=max(0, int(request.max_turns or 0)),
                        provider=request.provider_id,
                    )
                ),
            )
            del current
        except Exception:
            return


def _stop_reason_for_outcome(outcome: OperationOutcome) -> str:
    if outcome.status == "completed":
        return "done"
    if outcome.status == "aborted":
        return outcome.reason or "stopped"
    if outcome.status == "suspended":
        return outcome.reason or "approval"
    return outcome.reason or "error"

__all__ = ["TaskRuntime"]
