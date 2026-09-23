"""Typed runtime and task completion outcomes."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

OperationOutcomeStatus = Literal["completed", "failed", "aborted", "suspended"]


@dataclass(frozen=True)
class OperationOutcome:
    status: OperationOutcomeStatus
    reason: str = ""
    summary: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def completed(cls, *, summary: str = "", metadata: dict[str, Any] | None = None) -> OperationOutcome:
        return cls("completed", summary=summary, metadata=dict(metadata or {}))

    @classmethod
    def failed(
        cls,
        *,
        reason: str,
        summary: str = "",
        metadata: dict[str, Any] | None = None,
    ) -> OperationOutcome:
        return cls("failed", reason=reason, summary=summary, metadata=dict(metadata or {}))

    @classmethod
    def aborted(cls, *, reason: str = "", summary: str = "") -> OperationOutcome:
        return cls("aborted", reason=reason, summary=summary)

    @classmethod
    def suspended(
        cls,
        *,
        reason: str,
        summary: str = "",
        metadata: dict[str, Any] | None = None,
    ) -> OperationOutcome:
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
