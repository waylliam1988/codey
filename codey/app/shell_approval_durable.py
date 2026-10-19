"""Durable owner for shell approval lifecycle mutations.

The application facade delegates here so approval CAS and audit publication
stay separate from the rest of ``AppContext`` assembly.
"""

from __future__ import annotations

import logging
from typing import Any

from codey.durable.errors import CommitConflict
from codey.durable.model import ApprovalRecord, ApprovalStatus
from codey.runtime.effects.shell_approval_records import ShellApprovalFact

logger = logging.getLogger(__name__)


def record_fact(ctx: Any, fact: ShellApprovalFact) -> bool:
    changed = commit_fact(ctx, fact)
    if changed:
        try:
            ctx.shell_approval_records.append(fact)
        except OSError:
            logger.warning("shell approval audit projection unavailable: %s", fact.approval_id)
    return changed


def commit_fact(ctx: Any, fact: ShellApprovalFact) -> bool:
    durable = getattr(ctx, "durable", None)
    task_lookup = getattr(durable, "task", None)
    if durable is None or not callable(task_lookup):
        raise CommitConflict("shell approval requires an admitted durable task")
    try:
        task = task_lookup(fact.source_run_id)
    except (KeyError, OSError, TypeError, ValueError):
        raise CommitConflict("shell approval source task is not durable") from None
    if str(task.conversation_id) != fact.session_id:
        raise CommitConflict("shell approval conversation boundary mismatch")

    status_by_kind = {
        "requested": ApprovalStatus.REQUESTED,
        "approved": ApprovalStatus.APPROVED,
        "denied": ApprovalStatus.DENIED,
        "execution_claimed": ApprovalStatus.EXECUTION_CLAIMED,
        "execution_settled": ApprovalStatus.EXECUTION_SETTLED,
        "abandoned": ApprovalStatus.CANCELLED,
    }
    status = status_by_kind.get(fact.kind)
    continuation_by_kind = {
        "continuation_claimed": ("resuming", fact.continuation_run_id),
        "continuation_started": ("started", fact.continuation_run_id),
        "continuation_failed": ("failed", fact.continuation_run_id),
        "continuation_cancelled": ("cancelled", fact.continuation_run_id),
    }
    settlement = None
    if fact.kind in {"execution_settled", "denied"}:
        settlement = {
            "kind": fact.kind,
            "result_ref": fact.result_ref,
            "result_digest": fact.result_digest,
        }
    try:
        current = durable.approval(fact.approval_id)
    except KeyError:
        if status != ApprovalStatus.REQUESTED:
            raise CommitConflict("shell approval transition has no durable request") from None
        durable.create_approval(ApprovalRecord(
            approval_id=fact.approval_id,
            task_id=fact.source_run_id,
            conversation_id=fact.session_id,
            status=status,
            command_digest=fact.command_digest,
            command_ref=fact.command_ref,
            cwd=fact.cwd or ".",
            revision=0,
        ))
        return True
    if status is None:
        continuation = continuation_by_kind.get(fact.kind)
        if continuation is None:
            return False
        status = ApprovalStatus(current.status)
    continuation_status = continuation_by_kind.get(
        fact.kind, (current.continuation_status, current.continuation_run_id)
    )
    desired_settlement = settlement if settlement is not None else current.settlement
    if (
        current.status == status
        and current.continuation_status == continuation_status[0]
        and current.continuation_run_id == continuation_status[1]
        and current.settlement == desired_settlement
    ):
        return False
    durable.transition_approval(
        fact.approval_id,
        ApprovalRecord(
            approval_id=current.approval_id,
            task_id=current.task_id,
            conversation_id=current.conversation_id,
            status=status,
            command_digest=current.command_digest,
            command_ref=current.command_ref,
            cwd=current.cwd,
            revision=current.revision + 1,
            settlement=desired_settlement,
            continuation_status=continuation_status[0],
            continuation_run_id=continuation_status[1],
        ),
        expected_status=current.status,
    )
    return True


__all__ = ["commit_fact", "record_fact"]
