"""Small runtime scheduler over the append-only session log."""

from __future__ import annotations

from codey.runtime.cancellation import TaskCancelled
from codey.runtime.operation import Operation, OperationContext
from codey.runtime.outcome import OperationOutcome
from codey.runtime.session_log import RuntimeSessionLog


class OperationScheduler:
    def __init__(self, session_log: RuntimeSessionLog) -> None:
        self.session_log = session_log

    def run(self, session_id: str, run_id: str, operation: Operation) -> OperationOutcome:
        context = OperationContext(session_id=session_id, run_id=run_id, lane=operation.lane)
        self.session_log.append(
            session_id,
            lane=operation.lane,
            operation_id=operation.operation_id,
            kind="operation_started",
            payload={"operation_kind": operation.kind},
        )
        try:
            outcome = operation.run(context)
        except TaskCancelled as exc:
            outcome = OperationOutcome.aborted(
                reason="task_cancelled",
                summary=_bounded_exception_summary(exc),
            )
            self.settle(session_id, operation, outcome)
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
            self.settle(session_id, operation, outcome)
            raise
        self.settle(session_id, operation, outcome)
        return outcome

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


def _exception_reason(exc: Exception) -> str:
    name = type(exc).__name__.strip() or "operation_exception"
    return "".join(char.lower() if char.isalnum() else "_" for char in name).strip("_") or "operation_exception"


def _bounded_exception_summary(exc: Exception) -> str:
    return " ".join(str(exc or "").split())[:240]
