"""Pure approval CAS validation and folding into its task-owned checkpoint."""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import replace

from codey.durable.errors import CommitConflict
from codey.durable.model import (
    ApprovalRecord,
    ApprovalStatus,
    DurableMutation,
    TaskCheckpoint,
    thaw_json,
    valid_approval_transition,
)


def materialize_approval(mutation: DurableMutation, current: TaskCheckpoint) -> DurableMutation:
    """Return one mutation; approval facts never acquire a separate write path."""
    approval = mutation.approval
    assert approval is not None
    if current.conversation_id != mutation.conversation_id:
        raise CommitConflict("approval conversation boundary mismatch")
    raw = thaw_json(current.state.get("approvals", {}))
    if not isinstance(raw, dict):
        raise CommitConflict("task approval state is corrupt")
    existing_payload = raw.get(approval.approval_id)
    existing = (
        ApprovalRecord.from_payload(existing_payload)
        if isinstance(existing_payload, Mapping)
        else None
    )
    if existing is None:
        if approval.revision != 0 or approval.status != ApprovalStatus.REQUESTED:
            raise CommitConflict("new approval must start as requested revision zero")
        if mutation.expected_approval_status is not None:
            raise CommitConflict("new approval cannot have an expected status")
    else:
        expected = (
            ApprovalStatus(mutation.expected_approval_status)
            if mutation.expected_approval_status is not None
            else None
        )
        if expected != existing.status:
            raise CommitConflict("approval status changed")
        if approval.revision != existing.revision + 1:
            raise CommitConflict("approval revision changed")
        # A lifecycle annotation (for example continuation ownership) may
        # advance while the permission decision remains unchanged.  This
        # is still a durable CAS transition, but it must not require a
        # second JSONL history to explain the current state.
        if (
            ApprovalStatus(existing.status) != ApprovalStatus(approval.status)
            and not valid_approval_transition(ApprovalStatus(existing.status), ApprovalStatus(approval.status))
        ):
            raise CommitConflict(f"invalid approval transition: {existing.status} -> {approval.status}")
        if (
            approval.task_id != existing.task_id
            or approval.conversation_id != existing.conversation_id
            or approval.command_digest != existing.command_digest
            or approval.command_ref != existing.command_ref
            or approval.cwd != existing.cwd
        ):
            raise CommitConflict("approval identity is immutable")
    raw[approval.approval_id] = approval.to_payload()
    proposed = mutation.task
    if proposed is None:
        current_state = thaw_json(current.state)
        if not isinstance(current_state, Mapping):
            raise CommitConflict("task state is corrupt")
        proposed = replace(
            current,
            revision=current.revision + 1,
            state={**dict(current_state), "approvals": raw},
        )
    else:
        proposed_state = thaw_json(proposed.state)
        if not isinstance(proposed_state, dict):
            raise CommitConflict("task state is corrupt")
        proposed = replace(proposed, state={**dict(proposed_state), "approvals": raw})
    return replace(mutation, task=proposed, approval=None, expected_approval_status=None)

