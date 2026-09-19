"""Compatibility re-export: task-context assembly lives in operations.

Canonical home is codey.operations.task_context: assembling knowledge,
verification, and checkpoint state is orchestration, not workspace scanning.
Import from codey.operations.task_context in new code.
"""

from __future__ import annotations

from codey.operations.task_context import (
    CheckpointContext,
    ProjectTaskContext,
    ProjectTaskContextBuilder,
    safe_project_map,
    safe_verification_candidates,
)

__all__ = [
    "CheckpointContext",
    "ProjectTaskContext",
    "ProjectTaskContextBuilder",
    "safe_project_map",
    "safe_verification_candidates",
]
