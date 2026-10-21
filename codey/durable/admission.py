"""Atomic input admission and historical conversation/task forks."""

from __future__ import annotations

import hashlib
import json
import uuid
from collections.abc import Mapping, Sequence
from typing import TYPE_CHECKING

from codey.durable import conversation_ops
from codey.durable.errors import CommitConflict
from codey.durable.model import (
    CommitResult,
    CreateConversation,
    DurableMutation,
    EntryDraft,
    SubmissionDraft,
    SubmissionPlacement,
    SubmissionStatus,
    TaskCheckpoint,
    thaw_json,
)
from codey.durable.workflow import workflow_digest

if TYPE_CHECKING:
    from codey.durable.session import DurableSession


def message_metadata(state: Mapping[str, object] | None) -> dict[str, object]:
    raw = (state or {}).get("message_metadata", {})
    if not isinstance(raw, Mapping) or set(raw) - {"source", "in_reply_to_run_id"}:
        raise ValueError("invalid message metadata")
    result: dict[str, object] = {}
    if "source" in raw:
        source = raw["source"]
        if (not isinstance(source, Mapping) or set(source) != {"actor_id", "name", "kind"}
                or any(not isinstance(value, str) or not value or len(value) > 512 for value in source.values())):
            raise ValueError("invalid message source metadata")
        result["source"] = dict(source)
    if "in_reply_to_run_id" in raw:
        reply = raw["in_reply_to_run_id"]
        if not isinstance(reply, str) or len(reply) > 512:
            raise ValueError("invalid reply metadata")
        result["in_reply_to_run_id"] = reply
    return result


def admit_input(
    session: DurableSession, *, conversation_id: str, request_id: str, task_id: str,
    text: str, provider_snapshot: Mapping[str, object], task_state: Mapping[str, object] | None = None,
    initial_phase: str = "accepted", tool_results: tuple[Mapping[str, object], ...] = (),
    owner_task_id: str | None = None,
    abandon_on_restart: bool = False,
    workflow_name: str | None = None,
    workflow_version: int = 1,
    workflow_input: Mapping[str, object] | None = None,
    workflow_checkpoint: Mapping[str, object] | None = None,
    workflow_replay_contract: str = "deterministic-v1",
    creation: CreateConversation | None = None,
    placement: SubmissionPlacement | str = SubmissionPlacement.PLACED,
    mode: str = "follow_up",
) -> CommitResult:
    if not text.strip():
        raise ValueError("input text is required")
    metadata = message_metadata(task_state)
    placement = SubmissionPlacement(placement)
    if mode not in {"steer", "follow_up", "write"}:
        raise ValueError("unknown submission mode")
    if initial_phase not in {"accepted", "dispatching"}:
        raise ValueError("input admission phase must be accepted or dispatching")
    if owner_task_id is not None:
        if owner_task_id == task_id:
            raise ValueError("a task cannot own itself")
        try:
            session.store.task(owner_task_id)
        except KeyError as exc:
            raise ValueError(f"owner task does not exist: {owner_task_id}") from exc
    if type(abandon_on_restart) is not bool:
        raise ValueError("abandon_on_restart must be boolean")
    if workflow_name is not None and not workflow_name:
        raise ValueError("workflow name is required")
    if type(workflow_version) is not int or workflow_version < 1:
        raise ValueError("workflow version must be a positive integer")
    if not workflow_replay_contract:
        raise ValueError("workflow replay contract is required")
    workflow_input = dict(workflow_input or {})
    workflow_checkpoint = dict(workflow_checkpoint or {})
    if session.store.has_conversation(conversation_id):
        head, revision = session.store.head(conversation_id)
    else:
        head, revision = None, 0
        creation = creation or CreateConversation()
    fingerprint = _fingerprint({
        "text": text,
        "message_metadata": metadata,
        "provider_snapshot": provider_snapshot,
        "tool_results": tuple(dict(result) for result in tool_results),
        "workflow_origin": (task_state or {}).get("workflow_origin"),
        "owner_task_id": owner_task_id,
        "abandon_on_restart": abandon_on_restart,
        "workflow_name": workflow_name,
        "workflow_version": workflow_version,
        "workflow_input": workflow_input,
        "workflow_checkpoint": workflow_checkpoint,
        "workflow_replay_contract": workflow_replay_contract,
        "placement": placement,
        "mode": mode,
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
    entry = None
    if placement is SubmissionPlacement.PLACED:
        entry = EntryDraft(
            entry_id=f"entry-{uuid.uuid4().hex}",
            conversation_id=conversation_id,
            parent_id=head,
            kind="user_message",
            payload={"text": text, **metadata},
            by_task_id=task_id,
        )
        entries.append(entry)
    submission = SubmissionDraft(
        submission_id=f"submission-{uuid.uuid4().hex}",
        request_id=request_id,
        conversation_id=conversation_id,
        entry=entry,
        fingerprint=fingerprint,
        placement=placement,
        mode=mode,
        content={"text": text, "message_metadata": metadata},
    )
    state = {
        **dict(task_state or {}),
        "input_state": "queued" if placement is SubmissionPlacement.QUEUED else "placed",
        "input_text": text,
        "input_mode": mode,
        "owner_task_id": owner_task_id,
        "abandon_on_restart": abandon_on_restart,
    }
    if entry is not None:
        state["input_entry_id"] = entry.entry_id
    if workflow_name is not None:
        state.update({
            "workflow_name": workflow_name,
            "workflow_version": workflow_version,
            "workflow_input": workflow_input,
            "workflow_checkpoint": workflow_checkpoint,
            "workflow_replay_contract": workflow_replay_contract,
            "workflow_replay_digest": workflow_digest(
                workflow_name, workflow_version, workflow_input, workflow_checkpoint,
            ),
        })
    task = TaskCheckpoint(
        task_id=task_id,
        conversation_id=conversation_id,
        submission_id=submission.submission_id,
        phase=initial_phase,
        revision=0,
        provider_snapshot=provider_snapshot,
        state=state,
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


def admit_write(
    session: DurableSession, *, conversation_id: str, request_id: str,
    kind: str, data: Mapping[str, object], display_only: bool = True,
    model: Mapping[str, object] | None = None,
    edits: Sequence[Mapping[str, object]] | None = None,
    head: str | None = None,
    placement: SubmissionPlacement | str = SubmissionPlacement.PLACED,
) -> CommitResult:
    """Durably admit one passive entry write without creating a task."""
    if not kind or kind.startswith("pi."):
        raise ValueError("write kind must be non-empty and outside the reserved pi namespace")
    if not isinstance(data, Mapping):
        raise ValueError("write data must be an object")
    if type(display_only) is not bool:
        raise ValueError("display_only must be boolean")
    normalized_model, normalized_edits = normalize_passive_fields(model, edits)
    head_target = head
    if head_target is not None and (not isinstance(head_target, str) or not head_target):
        raise ValueError("head must be 'self' or a non-empty entry ID")
    if display_only and (normalized_model is not None or normalized_edits is not None or head_target is not None):
        raise ValueError("display-only writes cannot carry context metadata")
    placement = SubmissionPlacement(placement)
    content: dict[str, object] = {"kind": kind, "data": dict(data), "display_only": display_only}
    if normalized_model is not None:
        content["model"] = normalized_model
    if normalized_edits is not None:
        content["edits"] = normalized_edits
    if head_target is not None:
        content["head"] = head_target
    fingerprint = _fingerprint({
        "kind": kind,
        "data": content["data"],
        "display_only": display_only,
        "model": normalized_model,
        "edits": normalized_edits,
        "head": head_target,
        "placement": placement,
        "mode": "write",
    })
    existing = session.store.submission_result_for_request(conversation_id, request_id, fingerprint)
    if existing is not None:
        return existing
    if session.store.has_conversation(conversation_id):
        current_head, revision = session.store.head(conversation_id)
        creation = None
    else:
        current_head, revision = None, 0
        creation = CreateConversation()
    stale = False
    if head_target is not None and head_target != "self":
        if creation is not None:
            stale = True
        else:
            stale = head_target not in {
                entry.entry_id for entry in session.context(conversation_id)
            }
    entry = None
    if placement is SubmissionPlacement.PLACED and not stale:
        payload: dict[str, object] = {"data": dict(data), "display_only": display_only}
        if normalized_model is not None:
            payload["model"] = normalized_model
        if normalized_edits is not None:
            payload["edits"] = normalized_edits
        if head_target is not None:
            payload.update({"head": head_target, "head_marker": True})
        entry = EntryDraft(
            entry_id=f"entry-{uuid.uuid4().hex}",
            conversation_id=conversation_id,
            parent_id=current_head,
            kind=kind,
            payload=payload,
        )
    submission = SubmissionDraft(
        submission_id=f"submission-{uuid.uuid4().hex}",
        request_id=request_id,
        conversation_id=conversation_id,
        entry=entry,
        fingerprint=fingerprint,
        status=(
            SubmissionStatus.UNANSWERED
            if stale
            else SubmissionStatus.DONE
            if placement is SubmissionPlacement.PLACED
            else SubmissionStatus.QUEUED
        ),
        placement=SubmissionPlacement.QUEUED if stale else placement,
        mode="write",
        content=content,
        reason="stale" if stale else None,
    )
    return session._commit(DurableMutation(
        conversation_id=conversation_id,
        expected_revision=revision,
        entries=() if entry is None else (entry,),
        submission=submission,
        create_conversation=creation,
    ))


def normalize_passive_fields(
    model: Mapping[str, object] | None,
    edits: Sequence[Mapping[str, object]] | None,
) -> tuple[dict[str, object] | None, list[dict[str, object]] | None]:
    """Validate and copy the optional Pi-style passive entry metadata."""
    normalized_model: dict[str, object] | None = None
    if model is not None:
        if not isinstance(model, Mapping):
            raise ValueError("passive entry model must be an object")
        normalized_model = dict(model)

    normalized_edits: list[dict[str, object]] | None = None
    if edits is not None:
        if not isinstance(edits, Sequence):
            raise ValueError("passive entry edits must be a list")
        normalized_edits = []
        for raw_edit in edits:
            if not isinstance(raw_edit, Mapping):
                raise ValueError("passive entry edit must be an object")
            target = raw_edit.get("target")
            action = raw_edit.get("action")
            if not isinstance(target, str) or not target:
                raise ValueError("passive entry edit target is required")
            if action not in {"omit", "replace"}:
                raise ValueError(f"unknown passive entry edit action: {action!r}")
            if action == "replace":
                messages = raw_edit.get("messages")
                if not isinstance(messages, Sequence) or isinstance(messages, (str, bytes)):
                    raise ValueError("replacement context messages must be a list")
                if any(not isinstance(message, Mapping) for message in messages):
                    raise ValueError("replacement context message must be an object")
            normalized_edits.append(dict(raw_edit))
    return normalized_model, normalized_edits



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
