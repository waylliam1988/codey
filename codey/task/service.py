"""Public task service facade.

The implementation lives in ``codey.operations.task_flow`` so the task package
stays a submission boundary instead of accumulating provider, Ghost, research,
and completion orchestration imports.
"""

from __future__ import annotations

from codey.operations.task_flow import COMPLETION_REPAIR_FOLLOWUP, TaskFlow


class TaskService(TaskFlow):
    """Public entrypoint used by the app and tests."""


__all__ = ["COMPLETION_REPAIR_FOLLOWUP", "TaskService"]
