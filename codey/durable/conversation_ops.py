"""Conversation history and context operations for ``DurableSession``.

The durable session owns the mutation line and transaction publication.  This
module keeps the conversation tree, history cursor, and context snapshot
algorithms together so the task/effect coordinator remains small.
"""

from __future__ import annotations

import base64
import json
import uuid
from collections.abc import Callable, Mapping
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


def entries(session: Any, conversation_id: str) -> tuple[EntryDraft, ...]:
    return tuple(session.store.entries(conversation_id))


def history_page(
    session: Any,
    conversation_id: str,
    *,
    before: str | None = None,
    limit: int = 100,
) -> dict[str, object]:
    limit = max(1, min(200, int(limit)))
    path = [entry for entry in context(session, conversation_id) if entry.kind != "context_snapshot"]
    boundary = len(path)
    if before:
        decoded_cursor = _decode_history_cursor(before)
        if decoded_cursor[0] != conversation_id:
            raise ValueError("invalid conversation cursor")
        positions = {entry.entry_id: index for index, entry in enumerate(path)}
        if decoded_cursor[1] not in positions:
            raise ValueError("cursor does not belong to current conversation path")
        boundary = positions[decoded_cursor[1]]
    start = max(0, boundary - limit)
    selected = path[start:boundary]
    items = [_history_item(entry, start + index + 1) for index, entry in enumerate(selected)]
    has_more = start > 0
    cursor = _encode_history_cursor(conversation_id, selected[0].entry_id) if has_more and selected else None
    _head, revision = session.store.head(conversation_id)
    return {
        "schema_version": 1,
        "session_id": conversation_id,
        "epoch": session.database_id,
        "snapshot_revision": revision,
        "items": items,
        "before_cursor": cursor,
        "has_more": has_more,
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
    path = [entry for entry in context(session, conversation_id, leaf_id=leaf_id) if entry.kind != 'context_snapshot']
    position = next((index for index, entry in enumerate(path) if entry.entry_id == entry_id), None)
    if position is None:
        raise KeyError('entry is not on this conversation path')
    limit = max(1, min(200, int(limit)))
    start = max(0, position - limit // 2)
    end = min(len(path), start + limit)
    selected = path[start:end]
    return {
        'schema_version': 1, 'session_id': conversation_id, 'epoch': session.database_id,
        'snapshot_revision': session.store.head(conversation_id)[1],
        'items': [_history_item(entry, start + index + 1) for index, entry in enumerate(selected)],
        'has_more': start > 0, 'has_after': end < len(path),
        'before_cursor': _encode_history_cursor(conversation_id, selected[0].entry_id) if start else None,
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
    return _apply_head_cut(context_path(
        conversation_id, leaf,
        entries=session.store.entries, parent=session.store.conversation_parent,
    ))


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
    return _apply_head_cut(context_path(
        conversation_id,
        leaf,
        entries=visible,
        parent=session.store.conversation_parent,
    ))


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
        path = path[:positions[before_entry_id]]
    return build_context(path, token_budget=token_budget, token_counter=token_counter)


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


def _encode_history_cursor(conversation_id: str, entry_id: str) -> str:
    raw = json.dumps({"s": conversation_id, "e": entry_id}, separators=(",", ":")).encode("utf-8")
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _decode_history_cursor(value: str) -> tuple[str, str]:
    try:
        raw = base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))
        payload = json.loads(raw)
        if not isinstance(payload, dict) or not isinstance(payload.get("s"), str) or not isinstance(payload.get("e"), str):
            raise ValueError
        return payload["s"], payload["e"]
    except Exception as exc:
        raise ValueError("invalid conversation cursor") from exc


__all__ = [
    "append_message", "branch", "context", "context_at", "context_snapshot", "context_snapshot_at", "delete_conversation",
    "entries", "fork", "history_page", "latest_context_snapshot", "save_context_snapshot",
    "search_history",
]
