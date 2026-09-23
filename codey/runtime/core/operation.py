"""Runtime operation contracts.

Operations are the units scheduled by the runtime.  They carry bounded intent
and return a runtime outcome; verification, evidence, Ghost, provider sessions,
and HTTP state stay outside this module.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


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
