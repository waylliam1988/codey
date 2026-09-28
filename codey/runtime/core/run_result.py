"""Neutral task result type (no agent-loop dependency)."""
from __future__ import annotations

from dataclasses import dataclass


@dataclass
class RunResult:
    summary: str
    stop_reason: str = "done"
    turns: int = 0
    checks_passed: bool = False
    changed: bool = False
    checks_ran: bool = False


__all__ = ["RunResult"]
