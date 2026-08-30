"""Production task runtime entrypoint."""

from __future__ import annotations

from dataclasses import dataclass, field

from codey.runtime.operation import OperationContext, OperationIntent
from codey.runtime.outcome import OperationOutcome
from codey.runtime.ports import TaskExecutor, TaskPreparer
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

    def run(self, context: OperationContext) -> OperationOutcome:
        self.executor(self.request)
        return OperationOutcome.completed(summary="task settled")


class TaskRuntime:
    """Schedule submitted tasks through the runtime session log."""

    def __init__(
        self,
        session_log: RuntimeSessionLog,
        executor: TaskExecutor,
        *,
        prepare: TaskPreparer | None = None,
    ) -> None:
        self.session_log = session_log
        self.executor = executor
        self.prepare = prepare
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
        self.scheduler.run(request.session_id, run_id, operation)


__all__ = ["TaskRuntime"]
