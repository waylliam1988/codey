"""Execution diagnostics, independent of research proof quality conclusions."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Generic, Literal, TypeVar

AuditStatus = Literal["ok", "skipped", "error"]
AuditPhase = Literal["initial", "final"]
MAX_AUDIT_DIAGNOSTICS = 32
T = TypeVar("T")


@dataclass(frozen=True)
class AuditDiagnostic:
    stage: str
    status: AuditStatus
    phase: AuditPhase
    reason_code: str = ""
    error_type: str = ""

    def __post_init__(self) -> None:
        if self.status != "ok" and not self.reason_code:
            raise ValueError("Skipped and failed audits require a reason code")

    def to_payload(self) -> dict[str, object]:
        return {
            "stage": self.stage[:80],
            "status": self.status,
            "phase": self.phase,
            "reason_code": self.reason_code[:80],
            "error_type": self.error_type[:80],
        }


@dataclass(frozen=True)
class AuditOutcome(Generic[T]):
    diagnostic: AuditDiagnostic
    value: T | None = None

    def __post_init__(self) -> None:
        if (self.diagnostic.status == "ok") != (self.value is not None):
            raise ValueError("Only successful audits carry a result")
