"""Runtime operation contracts.

Operations are the units scheduled by the runtime.  They carry bounded intent
and return a runtime outcome; verification, evidence, Ghost, provider sessions,
and HTTP state stay outside this module.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal, Protocol

from codey.runtime.outcome import OperationOutcome

OperationStatus = Literal["pending", "running", "settled"]


@dataclass(frozen=True)
class OperationIntent:
    objective_ref: str
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class OperationContext:
    session_id: str
    run_id: str
    lane: str = "current"
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class OperationState:
    operation_id: str
    kind: str
    lane: str
    status: OperationStatus
    turns_used: int = 0


class Operation(Protocol):
    operation_id: str
    kind: str
    lane: str
    intent: OperationIntent

    def run(self, context: OperationContext) -> OperationOutcome:
        ...
