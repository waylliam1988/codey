"""Conversation history and context operations for ``DurableSession``.

The durable session owns the mutation line and transaction publication.  This
module keeps the conversation tree, history cursor, and context snapshot
algorithms together so the task/effect coordinator remains small.
"""

from __future__ import annotations

import base64
import json
import uuid
from collections.abc import Callable, Mapping, Sequence
from dataclasses import replace
from typing import Any, cast

from codey.durable.ancestry import context_path
from codey.durable.context import ContextSnapshot, build_context
from codey.durable.errors import CommitConflict
from codey.durable.model import (
    CommitResult,
    CreateConversation,
    DeleteConversation,
    DurableMutation,
    EntryDraft,
    thaw_json,
)
from codey.durable.task_projection import task_lifecycle


def entries(session: Any, conversation_id: str) -> tuple[EntryDraft, ...]:
    return _resolve_response_entries(session, tuple(session.store.entries(conversation_id)))


def _resolve_response_entries(session: Any, path: tuple[EntryDraft, ...]) -> tuple[EntryDraft, ...]:
    from codey.durable.provider_response import read_response_artifact, tool_call_message

    resolved = []
    for entry in path:
        reference = entry.payload.get("response_ref") if entry.kind == "tool_call" else None
        if reference is None:
            resolved.append(entry)
            continue
        if not isinstance(reference, str) or not reference or set(entry.payload) != {"response_ref"}:
            raise ValueError("invalid provider history reference")
        artifact = session.artifact(reference)
        if (artifact.owner_task_id != entry.by_task_id or not artifact.owner_effect_id
                or artifact.metadata.get("kind") != "provider_response"):
            raise ValueError("provider history reference ownership mismatch")
        envelope = read_response_artifact(artifact)
        if (not isinstance(envelope, dict) or envelope.get("schema_version") != 1
                or envelope.get("kind") != "provider_response"
                or envelope.get("effect_id") != artifact.owner_effect_id):
            raise ValueError("invalid provider history response")
        resolved.append(replace(entry, payload=tool_call_message(envelope.get("payload"))))
    return tuple(resolved)


def conversation_tree(session: Any, conversation_id: str) -> dict[str, object]:
    """Bounded branch metadata, derived under the owning session's read line."""
    head, revision = session.store.head(conversation_id)
    retained = list(session.store.entries(conversation_id))
    parent_scope, parent_cap = session.store.conversation_parent(conversation_id)
    if parent_scope and parent_cap:
        retained = list(context(session, parent_scope, leaf_id=parent_cap)) + retained
    parents = {entry.parent_id for entry in retained}
    tips = [entry for entry in retained if entry.entry_id not in parents or entry.entry_id == head]
    return {'ok': True, 'session_id': conversation_id, 'head_entry_id': head, 'revision': revision,
            'ancestors': conversation_lineage(session, conversation_id), 'has_more': len(tips) > 200,
            'branches': [{'entry_id': entry.entry_id, 'parent_entry_id': entry.parent_id,
                          'kind': entry.kind, 'text': str(entry.payload.get('text') or entry.payload.get('content') or entry.kind)[:240],
                          'current': entry.entry_id == head} for entry in tips[-200:]]}


def conversation_lineage(session: Any, conversation_id: str) -> list[dict[str, object]]:
    """Read fork identities without loading conversation contents."""
    scope = conversation_id
    ancestors: list[dict[str, object]] = []
    seen: set[str] = set()
    while scope and scope not in seen and len(seen) < 64:
        seen.add(scope)
        parent, entry = session.store.conversation_parent(scope)
        if parent:
            ancestors.append({'session_id': parent, 'entry_id': entry})
        scope = parent or ''
    return ancestors


def history_page(
    session: Any,
    conversation_id: str,
    *,
    before: str | None = None,
    limit: int = 100,
) -> dict[str, object]:
    limit = max(1, min(200, int(limit)))
    path = [entry for entry in context(session, conversation_id)
            if entry.kind not in {"context_snapshot", "control_config", "control_receipt"}]
    visible_runs = {entry.by_task_id for entry in path if entry.by_task_id}
    inactive_runs = sorted({entry.by_task_id for entry in session.store.entries(conversation_id)
                            if entry.by_task_id and entry.by_task_id not in visible_runs})
    _head, revision = session.store.head(conversation_id)
    boundary = len(path)
    if before:
        decoded_cursor = _decode_history_cursor(before)
        if decoded_cursor[0] != conversation_id:
            raise ValueError("invalid conversation cursor")
        positions = {entry.entry_id: index for index, entry in enumerate(path)}
        if decoded_cursor[2] is not None:
            if decoded_cursor[2] not in positions:
                raise ValueError("history snapshot changed")
            path = path[:positions[decoded_cursor[2]] + 1]
            positions = {entry.entry_id: index for index, entry in enumerate(path)}
            revision = decoded_cursor[3] if decoded_cursor[3] is not None else revision
        if decoded_cursor[1] not in positions:
            raise ValueError("cursor does not belong to current conversation path")
        boundary = positions[decoded_cursor[1]]
    start = max(0, boundary - limit)
    selected = path[start:boundary]
    items = [_history_item(entry, start + index + 1) for index, entry in enumerate(selected)]
    has_more = start > 0
    cursor = _encode_history_cursor(conversation_id, selected[0].entry_id,
                                    path[-1].entry_id, revision) if has_more and selected else None
    return {
        "schema_version": 1,
        "session_id": conversation_id,
        "epoch": session.database_id,
        "snapshot_revision": revision,
        "inactive_run_ids": inactive_runs,
        "items": items,
        "task_states": [task_lifecycle(session.store.task(run)) for run in sorted({entry.by_task_id for entry in selected if entry.by_task_id})],
        "before_cursor": cursor,
        "has_more": has_more,
    }


def navigation_page(session: Any, conversation_id: str, *, before: str | None = None,
                    limit: int = 200) -> dict[str, object]:
    """Compact question index on one pinned ancestry; never return tool bodies."""
    path = list(context(session, conversation_id))
    revision = session.store.head(conversation_id)[1]
    boundary = len(path)
    if before:
        scope, entry_id, head, pinned_revision = _decode_history_cursor(before)
        if scope != conversation_id:
            raise ValueError('invalid conversation cursor')
        positions = {entry.entry_id: index for index, entry in enumerate(path)}
        if head is not None:
            if head not in positions:
                raise ValueError('history snapshot changed')
            path = path[:positions[head] + 1]
            revision = pinned_revision if pinned_revision is not None else revision
        boundary = next((index for index, entry in enumerate(path) if entry.entry_id == entry_id), -1)
        if boundary < 0:
            raise ValueError('cursor does not belong to current conversation path')
    questions = [entry for entry in path[:boundary] if entry.kind == 'user_message']
    selected = questions[-max(1, min(200, int(limit))):]
    more = len(selected) < len(questions)
    return {
        'schema_version': 1, 'session_id': conversation_id, 'epoch': session.database_id,
        'snapshot_revision': revision,
        'items': [{'id': entry.entry_id, 'text': str(_history_item(entry, 0).get('text') or '')[:240]}
                  for entry in selected],
        'has_more': more,
        'before_cursor': _encode_history_cursor(conversation_id, selected[0].entry_id,
                                               path[-1].entry_id, revision) if more else None,
    }


def search_history(
    session: Any,
    conversation_id: str,
    query: str,
    *,
    limit: int = 50,
) -> list[dict[str, object]]:
    text = query.strip()
    if not text:
        return []
    limit = max(1, min(100, int(limit)))
    hits: list[dict[str, object]] = []
    visible_entries = (entry for entry in context(session, conversation_id) if entry.kind != "context_snapshot")
    for ordinal, entry in enumerate(visible_entries, 1):
        item = _history_item(entry, ordinal)
        body = str(item.get("text") or item.get("result") or item.get("output") or item.get("content") or "")
        index = body.casefold().find(text.casefold())
        if index < 0:
            continue
        hits.append({
            "entry_id": entry.entry_id,
            "message_id": entry.entry_id,
            "ordinal": ordinal,
            "excerpt": body[max(0, index - 80): index + len(text) + 80],
        })
        if len(hits) >= limit:
            break
    return hits


def history_window(session: Any, conversation_id: str, entry_id: str, *, limit: int = 100,
                   leaf_id: str | None = None) -> dict[str, object]:
    """Read a bounded window on one validated ancestry path without moving its head."""
    path = [entry for entry in context(session, conversation_id, leaf_id=leaf_id)
            if entry.kind not in {"context_snapshot", "control_config", "control_receipt"}]
    position = next((index for index, entry in enumerate(path) if entry.entry_id == entry_id), None)
    if position is None:
        raise KeyError('entry is not on this conversation path')
    limit = max(1, min(200, int(limit)))
    start = max(0, position - limit // 2)
    end = min(len(path), start + limit)
    selected = path[start:end]
    revision = session.store.head(conversation_id)[1]
    return {
        'schema_version': 1, 'session_id': conversation_id, 'epoch': session.database_id,
        'snapshot_revision': revision,
        'items': [_history_item(entry, start + index + 1) for index, entry in enumerate(selected)],
        'task_states': [task_lifecycle(session.store.task(run)) for run in sorted({entry.by_task_id for entry in selected if entry.by_task_id})],
        'has_more': start > 0, 'has_after': end < len(path),
        'before_cursor': _encode_history_cursor(conversation_id, selected[0].entry_id, path[-1].entry_id, revision) if start else None,
    }


def append_message(session: Any, conversation_id: str, kind: str, payload: Mapping[str, object]) -> EntryDraft:
    parent_id, revision = session.store.head(conversation_id)
    entry = EntryDraft(
        entry_id=f"entry-{uuid.uuid4().hex}", conversation_id=conversation_id,
        parent_id=parent_id, kind=kind, payload=payload,
    )
    result = session._commit(DurableMutation(conversation_id, revision, entries=(entry,)))
    return replace(entry, commit_seq=result.sequence)


def append_tool_results(
    session: Any,
    conversation_id: str,
    head: str | None,
    results: tuple[Mapping[str, object], ...],
    *,
    by_task_id: str,
) -> tuple[EntryDraft, ...]:
    """Build result entries for the currently open native tool turn.

    Approval continuation admission owns this transition.  The result is
    appended immediately after the assistant tool call and before the new
    user entry in the same durable mutation.  That ordering is required by
    both OpenAI protocol encoders; a separate result projection would leave a
    replayed context with an unanswered call or a duplicated result.
    """
    if not results:
        return ()
    path = context(session, conversation_id, leaf_id=head)
    pending: dict[str, EntryDraft] = {}
    for entry in path:
        if entry.kind == "tool_call":
            payload = thaw_json(entry.payload)
            calls = payload.get("tool_calls") if isinstance(payload, Mapping) else None
            for call in calls or ():
                if isinstance(call, Mapping) and isinstance(call.get("id"), str) and call["id"]:
                    pending[str(call["id"])] = entry
        elif entry.kind == "tool_result":
            payload = thaw_json(entry.payload)
            call_id = payload.get("call_id") if isinstance(payload, Mapping) else None
            if isinstance(call_id, str) and call_id:
                pending.pop(call_id, None)
    entries: list[EntryDraft] = []
    seen: set[str] = set()
    parent = head
    for raw in results:
        if not isinstance(raw, Mapping):
            raise CommitConflict("tool result must be an object")
        call_id = raw.get("call_id")
        content = raw.get("content", raw.get("text", ""))
        is_error = raw.get("is_error", False)
        if not isinstance(call_id, str) or not call_id:
            raise CommitConflict("tool result call ID is required")
        if call_id in seen or call_id not in pending:
            raise CommitConflict("tool result does not match an unanswered tool call")
        if not isinstance(content, str) or type(is_error) is not bool:
            raise CommitConflict("tool result payload is invalid")
        seen.add(call_id)
        entry = EntryDraft(
            entry_id=f"entry-{uuid.uuid4().hex}",
            conversation_id=conversation_id,
            parent_id=parent,
            kind="tool_result",
            payload={
                "call_id": call_id,
                "content": content,
                "is_error": is_error,
            },
            by_task_id=by_task_id,
        )
        entries.append(entry)
        parent = entry.entry_id
    return tuple(entries)


def branch(
    session: Any,
    conversation_id: str,
    entry_id: str,
    *,
    expected_revision: int | None = None,
) -> CommitResult:
    session._ensure_writable()
    try:
        _head, revision = session.store.head(conversation_id)
        captured_revision = revision if expected_revision is None else expected_revision
        return cast(CommitResult, session._commit(DurableMutation(
            conversation_id=conversation_id,
            expected_revision=captured_revision,
            branch_entry_id=entry_id,
        )))
    except (CommitConflict, KeyError, TypeError, ValueError):
        raise
    except Exception:
        session._sealed = True
        raise


def fork(session: Any, conversation_id: str, entry_id: str, *, child_conversation_id: str | None = None) -> str:
    child_id = child_conversation_id or f"conversation-{uuid.uuid4().hex}"
    session._commit(DurableMutation(
        conversation_id=child_id,
        expected_revision=0,
        create_conversation=CreateConversation(
            parent_conversation_id=conversation_id,
            parent_entry_id=entry_id,
        ),
    ))
    return child_id


def delete_conversation(session: Any, conversation_id: str) -> CommitResult:
    _head, revision = session.store.head(conversation_id)
    return cast(CommitResult, session._commit(DurableMutation(
        conversation_id=conversation_id,
        expected_revision=revision,
        delete_conversation=DeleteConversation(),
    )))


def context(session: Any, conversation_id: str, *, leaf_id: str | None = None) -> tuple[EntryDraft, ...]:
    leaf = leaf_id
    if leaf is None:
        leaf, _revision = session.store.head(conversation_id)
    return _resolve_response_entries(session, _apply_head_cut(context_path(
        conversation_id, leaf,
        entries=session.store.entries, parent=session.store.conversation_parent,
    )))


def context_at(
    session: Any,
    conversation_id: str,
    commit_seq: int,
    *,
    leaf_id: str | None = None,
) -> tuple[EntryDraft, ...]:
    """Rebuild a conversation path using only facts committed by ``commit_seq``."""
    if type(commit_seq) is not int or commit_seq < 0:
        raise ValueError("historical commit sequence must be a non-negative integer")

    def visible(scope: str) -> tuple[EntryDraft, ...]:
        return tuple(entry for entry in session.store.entries(scope) if entry.commit_seq <= commit_seq)

    local = visible(conversation_id)
    if leaf_id is None:
        leaf = local[-1].entry_id if local else session.store.conversation_parent(conversation_id)[1]
    else:
        leaf = leaf_id
    return _resolve_response_entries(session, _apply_head_cut(context_path(
        conversation_id,
        leaf,
        entries=visible,
        parent=session.store.conversation_parent,
    )))


def _apply_head_cut(path: tuple[EntryDraft, ...]) -> tuple[EntryDraft, ...]:
    """Apply the newest durable head marker to the visible context range."""
    for index in range(len(path) - 1, -1, -1):
        payload = thaw_json(path[index].payload)
        if not isinstance(payload, Mapping) or payload.get("head_marker") is not True:
            continue
        return path[index:]
    return path


def context_snapshot_at(
    session: Any,
    conversation_id: str,
    commit_seq: int,
    *,
    token_budget: int,
    leaf_id: str | None = None,
    token_counter: Callable[[tuple[Mapping[str, object], ...]], int] | None = None,
) -> ContextSnapshot:
    return build_context(
        context_at(session, conversation_id, commit_seq, leaf_id=leaf_id),
        token_budget=token_budget,
        token_counter=token_counter,
    )


def context_snapshot(
    session: Any,
    conversation_id: str,
    *,
    token_budget: int,
    leaf_id: str | None = None,
    before_entry_id: str | None = None,
    token_counter: Callable[[tuple[Mapping[str, object], ...]], int] | None = None,
) -> ContextSnapshot:
    path = context(session, conversation_id, leaf_id=leaf_id)
    if before_entry_id is not None:
        positions = {entry.entry_id: index for index, entry in enumerate(path)}
        if before_entry_id not in positions:
            raise CommitConflict("input entry does not belong to the selected context path")
        cutoff = positions[before_entry_id]
        prefix = path[:cutoff]
        # A compaction write is appended after the current input because it is
        # its own durable mutation.  It may still rewrite facts strictly
        # before that input.  Admit only edits with an explicit source fence
        # whose complete source set is already in the prefix; no target or
        # text inference is used here, so a future edit cannot leak backward.
        path = _include_committed_edits_before_cutoff(path, prefix, cutoff)
    return build_context(path, token_budget=token_budget, token_counter=token_counter)


def _include_committed_edits_before_cutoff(
    path: tuple[EntryDraft, ...], prefix: tuple[EntryDraft, ...], cutoff: int,
) -> tuple[EntryDraft, ...]:
    available = {entry.entry_id for entry in prefix}
    selected = list(prefix)
    for entry in path[cutoff:]:
        if entry.kind != "context_edit":
            continue
        payload = thaw_json(entry.payload)
        if not isinstance(payload, Mapping):
            continue
        data = payload.get("data")
        if not isinstance(data, Mapping):
            continue
        source_ids = data.get("source_entry_ids")
        if not isinstance(source_ids, Sequence) or isinstance(source_ids, (str, bytes)):
            continue
        normalized = tuple(item for item in source_ids if isinstance(item, str) and item)
        if not normalized or len(normalized) != len(source_ids) or not set(normalized) <= available:
            continue
        selected.append(entry)
        edits = payload.get("edits", payload.get("context_edits"))
        if not isinstance(edits, Sequence) or isinstance(edits, (str, bytes)):
            continue
        for raw_edit in edits:
            if not isinstance(raw_edit, Mapping) or raw_edit.get("action") != "replace":
                continue
            target = raw_edit.get("target")
            messages = raw_edit.get("messages")
            if not isinstance(target, str) or not isinstance(messages, Sequence) or isinstance(messages, (str, bytes)):
                continue
            available.update(f"{target}:replacement:{index}" for index, message in enumerate(messages)
                             if isinstance(message, Mapping))
    return tuple(selected)


def save_context_snapshot(session: Any, conversation_id: str, payload: Mapping[str, object]) -> EntryDraft:
    latest = latest_context_snapshot(session, conversation_id)
    if latest is not None and dict(latest.payload) == dict(payload):
        return latest
    parent_id, revision = session.store.head(conversation_id)
    entry = EntryDraft(
        entry_id=f"entry-{uuid.uuid4().hex}", conversation_id=conversation_id,
        parent_id=parent_id, kind="context_snapshot", payload=payload,
    )
    result = session._commit(DurableMutation(conversation_id, revision, entries=(entry,)))
    return replace(entry, commit_seq=result.sequence)


def latest_context_snapshot(session: Any, conversation_id: str) -> EntryDraft | None:
    for entry in reversed(context(session, conversation_id)):
        if entry.kind == "context_snapshot":
            return entry
    return None


def _history_item(entry: EntryDraft, ordinal: int) -> dict[str, object]:
    item = cast(dict[str, object], thaw_json(entry.payload))
    if entry.kind == "control_review":
        review = cast(dict[str, object], item.get("data", {}))
        verdict = "Review passed" if review.get("passed") else "Changes requested"
        item.update(type="info", text=f"{review.get('source', 'Reviewer')} · {verdict}: {review.get('summary', '')}")
    item.setdefault("type", {
        "user_message": "user", "assistant_message": "asst", "tool_call": "tool_call",
        "tool_result": "tool_result", "compaction": "info",
    }.get(entry.kind, entry.kind))
    item["id"] = entry.entry_id
    item["entry_id"] = entry.entry_id
    item["ordinal"] = ordinal
    if entry.by_task_id is not None:
        item["runId"] = entry.by_task_id
        if entry.kind == "assistant_message":
            item["finalAnswer"] = True
            item["contentRole"] = "final"
    return item


def _encode_history_cursor(conversation_id: str, entry_id: str, head: str | None = None,
                           revision: int | None = None) -> str:
    raw = json.dumps({"s": conversation_id, "e": entry_id, "h": head, "r": revision}, separators=(",", ":")).encode("utf-8")
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _decode_history_cursor(value: str) -> tuple[str, str, str | None, int | None]:
    try:
        raw = base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))
        payload = json.loads(raw)
        if not isinstance(payload, dict) or not isinstance(payload.get("s"), str) or not isinstance(payload.get("e"), str):
            raise ValueError
        head, revision = payload.get("h"), payload.get("r")
        if head is not None and (not isinstance(head, str) or not head or type(revision) is not int or revision < 0):
            raise ValueError
        return payload["s"], payload["e"], head, revision
    except Exception as exc:
        raise ValueError("invalid conversation cursor") from exc


__all__ = [
    "append_message", "branch", "context", "context_at", "context_snapshot", "context_snapshot_at", "delete_conversation",
    "entries", "fork", "history_page", "latest_context_snapshot", "save_context_snapshot",
    "search_history",
]
