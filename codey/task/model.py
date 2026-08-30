"""Task-facing data model.

This module is the boundary between user-visible task submission protocols and
the internal runtime.  It deliberately contains no provider, server, Ghost, or
tool execution imports.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

TaskKind = Literal["chat", "project", "research", "review", "planning"]


@dataclass(frozen=True)
class TaskSubmission:
    session_id: str
    project: str | None
    task: str
    max_turns: int
    continue_task: bool
    provider_id: str
    intent: str = "auto"
    run_id: str = ""


@dataclass(frozen=True)
class TaskContract:
    session_id: str
    run_id: str
    kind: TaskKind | str
    project: str | None
    prompt: str
    max_turns: int
    provider_id: str
    continue_task: bool = False
    intent: str = "auto"


@dataclass(frozen=True)
class TaskState:
    contract: TaskContract
    turns_used: int = 0
    terminal: bool = False
    completion_status: str = ""
