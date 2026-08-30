"""Typed runtime and task completion outcomes."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

OperationOutcomeStatus = Literal["completed", "failed", "aborted", "suspended"]
TaskCompletionStatus = Literal[
    "complete",
    "complete_with_limitations",
    "incomplete",
    "blocked",
    "failed",
]


@dataclass(frozen=True)
class OperationOutcome:
    status: OperationOutcomeStatus
    reason: str = ""
    summary: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def completed(cls, *, summary: str = "", metadata: dict[str, Any] | None = None) -> "OperationOutcome":
        return cls("completed", summary=summary, metadata=dict(metadata or {}))

    @classmethod
    def failed(
        cls,
        *,
        reason: str,
        summary: str = "",
        metadata: dict[str, Any] | None = None,
    ) -> "OperationOutcome":
        return cls("failed", reason=reason, summary=summary, metadata=dict(metadata or {}))

    @classmethod
    def aborted(cls, *, reason: str = "", summary: str = "") -> "OperationOutcome":
        return cls("aborted", reason=reason, summary=summary)

    @classmethod
    def suspended(
        cls,
        *,
        reason: str,
        summary: str = "",
        metadata: dict[str, Any] | None = None,
    ) -> "OperationOutcome":
        return cls("suspended", reason=reason, summary=summary, metadata=dict(metadata or {}))

    def to_payload(self) -> dict[str, Any]:
        payload: dict[str, Any] = {"outcome": self.status}
        if self.reason:
            payload["reason"] = self.reason
        if self.summary:
            payload["summary"] = self.summary
        if self.metadata:
            payload["metadata"] = dict(self.metadata)
        return payload


def operation_outcome_from_stop_reason(
    stop_reason: str,
    *,
    blocked_reason: str = "",
    summary: str = "",
) -> OperationOutcome:
    reason = str(stop_reason or "")
    blocked = str(blocked_reason or "")
    if reason == "done":
        return OperationOutcome.completed(summary=summary or "done")
    if reason == "stopped":
        return OperationOutcome.aborted(reason="stopped")
    if reason == "approval":
        return OperationOutcome.suspended(reason="approval")
    if blocked:
        return OperationOutcome.failed(reason=blocked, summary=summary)
    if reason:
        return OperationOutcome.failed(reason=reason, summary=summary)
    return OperationOutcome.completed(summary=summary)


@dataclass(frozen=True)
class CompletionVerdict:
    status: TaskCompletionStatus
    reason_codes: tuple[str, ...] = ()
    summary: str = ""
    repair_admitted: bool = False

    @property
    def blocked_reason(self) -> str:
        if self.status != "blocked" or not self.reason_codes:
            return ""
        return self.reason_codes[0]

    def to_payload(self) -> dict[str, Any]:
        return {
            "completion_status": self.status,
            "reason_codes": list(self.reason_codes),
            "summary": self.summary,
            "repair_admitted": self.repair_admitted,
        }
