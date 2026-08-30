"""Pure task-mode dispatch."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from codey.operations.context import RunFrame, RunHooks, RunWork
from codey.operations.result import ModeOutcome
from codey.workspace.config import ProjectConfigLoadResult


@dataclass(frozen=True)
class ModeDispatchDeps:
    chat: Callable[[RunFrame], ModeOutcome]
    project: Callable[..., ModeOutcome]
    research: Callable[[RunFrame, RunHooks], ModeOutcome]
    hybrid: Callable[[RunFrame, RunWork, RunHooks], ModeOutcome]
    review: Callable[[RunFrame], ModeOutcome]
    planning: Callable[..., ModeOutcome]


def dispatch_task_mode(
    kind: str,
    frame: RunFrame,
    work: RunWork,
    hooks: RunHooks,
    deps: ModeDispatchDeps,
    *,
    config_result: ProjectConfigLoadResult,
) -> ModeOutcome:
    if kind == "review":
        return deps.review(frame)
    if kind == "research":
        return deps.research(frame, hooks)
    if kind == "hybrid":
        return deps.hybrid(frame, work, hooks)
    if kind == "planning_readonly":
        return deps.planning(frame, work, config_result=config_result)
    if kind == "project":
        return deps.project(frame, work, hooks, config_result=config_result)
    return deps.chat(frame)


__all__ = ["ModeDispatchDeps", "dispatch_task_mode"]
