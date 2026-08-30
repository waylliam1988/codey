"""Small runtime scheduler over the append-only session log."""

from __future__ import annotations

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
        outcome = operation.run(context)
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
