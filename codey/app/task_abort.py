"""Interrupt physical resources after the scheduler's durable cancel barrier."""
from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from codey.durable.model import TaskCheckpoint

if TYPE_CHECKING:
    from codey.app.context import AppContext

logger = logging.getLogger(__name__)


def abort_task_resources(ctx: AppContext, checkpoint: TaskCheckpoint) -> None:
    """Drive the durable cancel barrier into live provider and shell work."""
    run_id = str(checkpoint.task_id or "")
    if not run_id:
        return
    # Provider transports and shell waits observe this exact run-scoped
    # event through their cancellation scopes.  The durable scheduler owns
    # the checkpoint transition; this handler only interrupts live work.
    ctx.run_registry.request_stop(run_id)
    provider = ctx._active_providers.get(run_id)
    abandon = getattr(provider, "abandon_inflight", None)
    if callable(abandon):
        try:
            abandon()
        except Exception as exc:
            logger.debug("provider abort cleanup failed for %s: %s", run_id, exc)
    events: tuple[dict[str, Any], ...] = ()
    with ctx._shell_spawn_gate, ctx.lock:
        expire = getattr(ctx, "_expire_shell_results_locked", None)
        if callable(expire):
            events = tuple(expire(run_id=run_id))
        workflow = getattr(ctx, "shell_approval_workflow", None)
        cancel_queued = getattr(workflow, "cancel_queued", None)
        if callable(cancel_queued):
            cancel_queued(checkpoint.conversation_id)
    for event in events:
        ctx.record_shell_result(event)

