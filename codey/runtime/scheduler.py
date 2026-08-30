"""Small runtime scheduler over the append-only session log."""

from __future__ import annotations

from codey.runtime.cancellation import TaskCancelled
from codey.runtime.operation import Operation, OperationContext
from codey.runtime.outcome import OperationOutcome
from codey.runtime.reducer import reduce_session
from codey.runtime.session_log import RuntimeSessionLog


class OperationScheduler:
    def __init__(self, session_log: RuntimeSessionLog) -> None:
        self.session_log = session_log

    def run(self, session_id: str, run_id: str, operation: Operation) -> OperationOutcome:
        context = OperationContext(session_id=session_id, run_id=run_id, lane=operation.lane)
        self.ensure_open(session_id, operation)
        try:
            outcome = operation.run(context)
        except TaskCancelled as exc:
            outcome = OperationOutcome.aborted(
                reason="task_cancelled",
                summary=_bounded_exception_summary(exc),
            )
            self.settle_if_open(session_id, operation, outcome)
            raise
        except Exception as exc:
            if type(exc).__name__ == "ControlTeachCancelled":
                outcome = OperationOutcome.aborted(
                    reason="control_teach_cancelled",
                    summary=_bounded_exception_summary(exc),
                )
            else:
                outcome = OperationOutcome.failed(
                    reason=_exception_reason(exc),
                    summary=_bounded_exception_summary(exc),
                )
            self.settle_if_open(session_id, operation, outcome)
            raise
        self.settle_if_open(session_id, operation, outcome)
        return outcome

    def ensure_open(self, session_id: str, operation: Operation) -> None:
        projection = reduce_session(self.session_log.read(session_id))
        existing = projection.operations.get(operation.operation_id)
        if existing is not None:
            if existing.lane != operation.lane:
                raise RuntimeError("operation lane changed")
            if existing.status != "open":
                raise RuntimeError("operation already settled")
            return
        lane = projection.lanes.get(operation.lane)
        if lane is not None and lane.open_operation_id:
            raise RuntimeError("lane already has an open operation")
        self.session_log.append(
            session_id,
            lane=operation.lane,
            operation_id=operation.operation_id,
            kind="operation_started",
            payload={"operation_kind": operation.kind},
        )

    def record_effect(
        self,
        session_id: str,
        operation: Operation,
        *,
        effect_kind: str,
        ref: str,
    ) -> None:
        self.session_log.append(
            session_id,
            lane=operation.lane,
            operation_id=operation.operation_id,
            kind="operation_effect",
            payload={"effect_kind": effect_kind, "ref": ref},
        )

    def settle(
        self,
        session_id: str,
        operation: Operation,
        outcome: OperationOutcome,
    ) -> None:
        self.session_log.append(
            session_id,
            lane=operation.lane,
            operation_id=operation.operation_id,
            kind="operation_settled",
            payload=outcome.to_payload(),
        )

    def settle_if_open(
        self,
        session_id: str,
        operation: Operation,
        outcome: OperationOutcome,
    ) -> None:
        projection = reduce_session(self.session_log.read(session_id))
        current = projection.operations.get(operation.operation_id)
        if current is None or current.status != "open":
            return
        self.settle(session_id, operation, outcome)


def _exception_reason(exc: Exception) -> str:
    name = type(exc).__name__.strip() or "operation_exception"
    return "".join(char.lower() if char.isalnum() else "_" for char in name).strip("_") or "operation_exception"


def _bounded_exception_summary(exc: Exception) -> str:
    return " ".join(str(exc or "").split())[:240]
