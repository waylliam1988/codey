"""Production task runtime entrypoint."""

from __future__ import annotations

from dataclasses import dataclass, field

from codey.runtime.operation import OperationContext, OperationIntent
from codey.runtime.outcome import OperationOutcome
from codey.runtime.ports import TaskExecutor, TaskPreparer, TaskStartFailureHandler
from codey.runtime.scheduler import OperationScheduler
from codey.runtime.session_log import RuntimeSessionLog
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
        self.scheduler = OperationScheduler(session_log)

    def run(self, request: TaskSubmission) -> None:
        if self.prepare is not None:
            request = self.prepare(request)
            if request is None:
                return
        if not request.run_id:
            raise ValueError("runtime task submissions require a run_id")
        # The detailed phase projection is recorded by RuntimeOperationStore
        # on the same session log. The scheduler still owns the invariant that
        # a started runtime operation is settled even when the executor raises.
        run_id = request.run_id
        operation = _SubmittedTaskOperation(
            request=request,
            executor=self.executor,
            operation_id=f"runtime:{run_id}",
            kind="task",
            lane=f"runtime:{run_id}",
            intent=OperationIntent(f"task:{run_id}"),
        )
        try:
            self.scheduler.run(request.session_id, run_id, operation)
        except Exception:
            if not operation.entered and self.on_unstarted_failure is not None:
                self.on_unstarted_failure(request)
            raise


__all__ = ["TaskRuntime"]
