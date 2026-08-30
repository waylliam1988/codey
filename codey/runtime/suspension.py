"""First-class suspended operation state."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

SuspensionReason = Literal[
    "user_deferred",
    "provider_failure",
    "missing_capability",
    "crash",
    "verification_blocked",
    "resource_unavailable",
]


@dataclass(frozen=True)
class SuspendedOperation:
    operation_id: str
    reason: SuspensionReason | str
    state_snapshot_ref: str
    continuation_ref: str
    metadata: dict[str, Any] = field(default_factory=dict)
