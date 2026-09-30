"""Neutral task result type (no agent-loop dependency)."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class RunResult:
    summary: str
    stop_reason: str = "done"
    turns: int = 0
    checks_passed: bool = False
    changed: bool = False
    checks_ran: bool = False
    # 共同 gate 的权威证明：投影与外层不得丢弃后另造。
    proof: Any | None = None
    # In-memory task facts shared with post-review completion; never serialized.
    facts: Any | None = field(default=None, repr=False, compare=False)


__all__ = ["RunResult"]
