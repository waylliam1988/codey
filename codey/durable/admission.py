"""Atomic input admission and historical conversation/task forks."""

from __future__ import annotations

import hashlib
import json
import uuid
from collections.abc import Mapping
from typing import TYPE_CHECKING

from codey.durable import conversation_ops
from codey.durable.errors import CommitConflict
from codey.durable.model import (
    CommitResult,
    CreateConversation,
    DurableMutation,
    EntryDraft,
    SubmissionDraft,
    TaskCheckpoint,
    thaw_json,
)

if TYPE_CHECKING:
    from codey.durable.session import DurableSession


def admit_input(
    session: DurableSession, *, conversation_id: str, request_id: str, task_id: str,
    text: str, provider_snapshot: Mapping[str, object], task_state: Mapping[str, object] | None = None,
    initial_phase: str = "accepted", tool_results: tuple[Mapping[str, object], ...] = (),
    creation: CreateConversation | None = None,
) -> CommitResult:
    if not text.strip():
        raise ValueError("input text is required")
    if initial_phase not in {"accepted", "dispatching"}:
        raise ValueError("input admission phase must be accepted or dispatching")
    if session.store.has_conversation(conversation_id):
        head, revision = session.store.head(conversation_id)
    else:
        head, revision = None, 0
        creation = creation or CreateConversation()
    fingerprint = _fingerprint({
        "text": text,
        "provider_snapshot": provider_snapshot,
        "tool_results": tuple(dict(result) for result in tool_results),
        "workflow_origin": (task_state or {}).get("workflow_origin"),
    })
    # Check idempotency before deriving any branch entries.  A retry of a
    # continuation must return the original commit without trying to
    # append its already-consumed tool result a second time.
    existing = session.store.submission_result_for_request(
        conversation_id, request_id, fingerprint,
    )
    if existing is not None:
        return existing
    if creation is not None and tool_results:
        raise CommitConflict("a new conversation has no unanswered tool calls")
    entries = list(
        conversation_ops.append_tool_results(
            session,
            conversation_id,
            head,
            tool_results,
            by_task_id=task_id,
        )
    )
    if entries:
        head = entries[-1].entry_id
    entry = EntryDraft(
        entry_id=f"entry-{uuid.uuid4().hex}",
        conversation_id=conversation_id,
        parent_id=head,
        kind="user_message",
        payload={"text": text},
        by_task_id=task_id,
    )
    entries.append(entry)
    submission = SubmissionDraft(
        submission_id=f"submission-{uuid.uuid4().hex}",
        request_id=request_id,
        conversation_id=conversation_id,
        entry=entry,
        fingerprint=fingerprint,
    )
    task = TaskCheckpoint(
        task_id=task_id,
        conversation_id=conversation_id,
        submission_id=submission.submission_id,
        phase=initial_phase,
        revision=0,
        provider_snapshot=provider_snapshot,
        state={"input_entry_id": entry.entry_id, **dict(task_state or {})},
    )
    return session._commit(
        DurableMutation(
            conversation_id=conversation_id,
            expected_revision=revision,
            entries=tuple(entries),
            submission=submission,
            task=task,
            create_conversation=creation,
        )
    )



def fork_task(
    session: DurableSession, source_task_id: str, commit_seq: int, *,
    conversation_id: str, task_id: str, request_id: str, instruction: str,
) -> CommitResult:
    snapshot = session.task_at(source_task_id, commit_seq)
    if snapshot.head_entry_id is None:
        raise CommitConflict("historical task has no conversation entry")
    if any(effect.settlement is None for effect in snapshot.effects):
        raise CommitConflict("cannot fork a checkpoint with unsettled external effects")
    source = snapshot.task
    # New execution inherits authorization and provider identity, never old
    # approval consumption, attempt ownership, delivery state or final proof.
    preserved = {
        key: source.state[key] for key in (
            "project", "max_turns", "provider_id", "intent", "task_kind", "requested_capabilities",
            "strict_research", "sources_open_required", "project_changes_required", "denied_capabilities",
            "model_selection", "task_policy", "reviewer_selection",
        ) if key in source.state
    }
    return admit_input(
        session, conversation_id=conversation_id, request_id=request_id, task_id=task_id,
        text=instruction, provider_snapshot=source.provider_snapshot,
        task_state={**preserved, "task": instruction, "continue_task": False,
                    "workflow_origin": {"task_id": source_task_id, "commit_seq": snapshot.commit_seq}},
        creation=CreateConversation(source.conversation_id, snapshot.head_entry_id),
    )


def _fingerprint(value: Mapping[str, object]) -> str:
    encoded = json.dumps(thaw_json(value), ensure_ascii=False, separators=(",", ":"), sort_keys=True, allow_nan=False)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()
