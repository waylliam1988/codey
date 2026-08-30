"""Ports used by the task runtime.

Runtime orchestration depends on these protocols instead of importing HTTP
handlers or concrete app registries.
"""

from __future__ import annotations

from typing import Protocol

from codey.runtime.outcome import OperationOutcome
from codey.task.model import TaskSubmission


class TaskExecutor(Protocol):
    def __call__(self, request: TaskSubmission) -> OperationOutcome | None:
        ...


class TaskPreparer(Protocol):
    def __call__(self, request: TaskSubmission) -> TaskSubmission | None:
        ...


class TaskStartFailureHandler(Protocol):
    def __call__(self, request: TaskSubmission) -> None:
        ...


class TaskRuntimePort(Protocol):
    def run(self, request: TaskSubmission) -> None:
        ...


__all__ = [
    "TaskExecutor",
    "TaskPreparer",
    "TaskRuntimePort",
    "TaskStartFailureHandler",
]
