"""Conversation history and context operations for ``DurableSession``.

The durable session owns the mutation line and transaction publication.  This
module keeps the conversation tree, history cursor, and context snapshot
algorithms together so the task/effect coordinator remains small.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable, Mapping
from dataclasses import replace

from codey.durable import history_queries
from codey.durable.ancestry import context_path
from codey.durable.context import ContextSnapshot, build_context
from codey.durable.errors import CommitConflict
from codey.durable.history_projection import _history_item as _history_item
from codey.durable.history_projection import _original_provider_window as _original_provider_window
from codey.durable.history_projection import _resolve_response_entries as _resolve_response_entries
from codey.durable.model import (
    CommitResult,
    CreateConversation,
    DeleteConversation,
    DurableMutation,
    EntryDraft,
)
from codey.durable.session_kernel import SessionKernel
from codey.utils.object_fields import mapping_fields, sequence_items


def entries(session: SessionKernel, conversation_id: str) -> tuple[EntryDraft, ...]:
    return _resolve_response_entries(session, tuple(session.store.entries(conversation_id)))


def conversation_tree(session: SessionKernel, conversation_id: str) -> dict[str, object]:
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


def conversation_lineage(session: SessionKernel, conversation_id: str) -> list[dict[str, object]]:
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


def history_page(session: SessionKernel, conversation_id: str, *, before: str | None = None,
                 limit: int = 100) -> dict[str, object]:
    return history_queries.history_page(session, conversation_id, before=before, limit=limit)


def navigation_page(session: SessionKernel, conversation_id: str, *, before: str | None = None,
                    limit: int = 200) -> dict[str, object]:
    return history_queries.navigation_page(session, conversation_id, before=before, limit=limit)


def search_history(
    session: SessionKernel,
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


def history_window(session: SessionKernel, conversation_id: str, entry_id: str, *, limit: int = 100,
                   leaf_id: str | None = None) -> dict[str, object]:
    return history_queries.history_window(session, conversation_id, entry_id, limit=limit, leaf_id=leaf_id)


def append_message(session: SessionKernel, conversation_id: str, kind: str, payload: Mapping[str, object]) -> EntryDraft:
    parent_id, revision = session.store.head(conversation_id)
    entry = EntryDraft(
        entry_id=f"entry-{uuid.uuid4().hex}", conversation_id=conversation_id,
        parent_id=parent_id, kind=kind, payload=payload,
    )
    result = session._commit(DurableMutation(conversation_id, revision, entries=(entry,)))
    return replace(entry, commit_seq=result.sequence)


def append_tool_results(
    session: SessionKernel,
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
    pending: set[str] = set()
    for entry in path:
        if entry.kind == "tool_call":
            calls = entry.payload.get("tool_calls")
            if sequence_items(calls):
                for call in calls:
                    if mapping_fields(call) and isinstance(call_id := call.get("id"), str) and call_id:
                        pending.add(call_id)
        elif entry.kind == "tool_result":
            call_id = entry.payload.get("call_id")
            if isinstance(call_id, str) and call_id:
                pending.discard(call_id)
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
    session: SessionKernel,
    conversation_id: str,
    entry_id: str,
    *,
    expected_revision: int | None = None,
) -> CommitResult:
    session._ensure_writable()
    try:
        _head, revision = session.store.head(conversation_id)
        captured_revision = revision if expected_revision is None else expected_revision
        return session._commit(DurableMutation(
            conversation_id=conversation_id,
            expected_revision=captured_revision,
            branch_entry_id=entry_id,
        ))
    except (CommitConflict, KeyError, TypeError, ValueError):
        raise
    except Exception:
        session._sealed = True
        raise


def fork(session: SessionKernel, conversation_id: str, entry_id: str, *, child_conversation_id: str | None = None) -> str:
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


def delete_conversation(session: SessionKernel, conversation_id: str) -> CommitResult:
    _head, revision = session.store.head(conversation_id)
    return session._commit(DurableMutation(
        conversation_id=conversation_id,
        expected_revision=revision,
        delete_conversation=DeleteConversation(),
    ))


def context(session: SessionKernel, conversation_id: str, *, leaf_id: str | None = None) -> tuple[EntryDraft, ...]:
    leaf = leaf_id
    if leaf is None:
        leaf, _revision = session.store.head(conversation_id)
    return _resolve_response_entries(session, _apply_head_cut(context_path(
        conversation_id, leaf,
        entries=session.store.entries, parent=session.store.conversation_parent,
    )))


def context_at(
    session: SessionKernel,
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
        if path[index].payload.get("head_marker") is True:
            return path[index:]
    return path


def context_snapshot_at(
    session: SessionKernel,
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
    session: SessionKernel,
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
        payload = entry.payload
        data = payload.get("data")
        if not mapping_fields(data):
            continue
        source_ids = data.get("source_entry_ids")
        if not sequence_items(source_ids):
            continue
        normalized = tuple(item for item in source_ids if isinstance(item, str) and item)
        if not normalized or len(normalized) != len(source_ids) or not set(normalized) <= available:
            continue
        selected.append(entry)
        edits = payload.get("edits", payload.get("context_edits"))
        if not sequence_items(edits):
            continue
        for raw_edit in edits:
            if not mapping_fields(raw_edit) or raw_edit.get("action") != "replace":
                continue
            target = raw_edit.get("target")
            messages = raw_edit.get("messages")
            if not isinstance(target, str) or not sequence_items(messages):
                continue
            available.update(f"{target}:replacement:{index}" for index, message in enumerate(messages)
                             if mapping_fields(message))
    return tuple(selected)


def save_context_snapshot(session: SessionKernel, conversation_id: str, payload: Mapping[str, object]) -> EntryDraft:
    latest = latest_context_snapshot(session, conversation_id)
    parent_id, revision = session.store.head(conversation_id)
    entry = EntryDraft(
        entry_id=f"entry-{uuid.uuid4().hex}", conversation_id=conversation_id,
        parent_id=parent_id, kind="context_snapshot", payload=payload,
    )
    if latest is not None and latest.payload == entry.payload:
        return latest
    result = session._commit(DurableMutation(conversation_id, revision, entries=(entry,)))
    return replace(entry, commit_seq=result.sequence)


def latest_context_snapshot(session: SessionKernel, conversation_id: str) -> EntryDraft | None:
    for entry in reversed(context(session, conversation_id)):
        if entry.kind == "context_snapshot":
            return entry
    return None


__all__ = [
    "append_message", "branch", "context", "context_at", "context_snapshot", "context_snapshot_at", "delete_conversation",
    "entries", "fork", "history_page", "latest_context_snapshot", "save_context_snapshot",
    "search_history",
]
