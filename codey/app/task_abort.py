"""Interrupt physical resources after the scheduler's durable cancel barrier."""
from __future__ import annotations

from typing import TYPE_CHECKING

from codey.durable.model import TaskCheckpoint

if TYPE_CHECKING:
    from codey.app.context import AppContext



def abort_task_resources(ctx: AppContext, checkpoint: TaskCheckpoint) -> None:
    """Drive the durable cancel barrier into live provider and shell work."""
    run_id = str(checkpoint.task_id or "")
    if not run_id:
        return
    ctx.task_runtime.abort_provider(run_id)
    ctx.task_lifecycle.abort_approvals(checkpoint)
