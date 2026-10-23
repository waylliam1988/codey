"""Validate a history edit against reads bound to the committing transaction."""
from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import TYPE_CHECKING

from codey.durable.errors import CommitConflict
from codey.durable.model import DurableMutation, EntryDraft, TaskCheckpoint

if TYPE_CHECKING:
    from codey.durable.session import DurableSession


def prepare_edit(session: DurableSession, conversation_id: str, revision: int, edit: Mapping[str, object],
                 *, placed: bool, has_tool_results: bool) -> tuple[str | None, str | None, tuple[str, ...]]:
    if revision != edit.get("expected_revision") or not session.store.has_conversation(conversation_id):
        raise CommitConflict("Conversation changed. Cancel the edit and try again.")
    if has_tool_results or not placed:
        raise ValueError("an edit must place one new user input")
    parent, replaced = edit_parent(str(edit.get("entry_id") or ""), session.context(conversation_id),
        busy=any(task.conversation_id == conversation_id for task in session.store.pending_tasks()))
    local_ids = {entry.entry_id for entry in session.store.entries(conversation_id)}
    return parent if parent in local_ids else None, parent, replaced


def edit_parent(entry_id: str | None, path: Sequence[EntryDraft], *, busy: bool) -> tuple[str | None, tuple[str, ...]]:
    if busy:
        raise CommitConflict("Stop the current task before editing a message.")
    index = next((i for i, entry in enumerate(path) if entry.entry_id == entry_id), None)
    if index is None or path[index].kind != "user_message":
        raise CommitConflict("The message is no longer on the current conversation branch.")
    parent = path[index - 1].entry_id if index else None
    replaced = tuple(sorted({entry.by_task_id for entry in path[index:] if entry.by_task_id}))
    return parent, replaced


def validate_edit(mutation: DurableMutation, path: Sequence[EntryDraft], *, busy: bool) -> str | None:
    parent, replaced = edit_parent(mutation.edited_entry_id, path, busy=busy)
    if mutation.replaced_run_ids != replaced:
        raise CommitConflict("conversation branch changed")
    if mutation.entries[0].payload.get("edited_entry_id") != mutation.edited_entry_id:
        raise CommitConflict("replacement input must identify its source")
    if mutation.entries[0].payload.get("edit_parent_entry_id") != parent:
        raise CommitConflict("replacement input must identify its prefix")
    local_ids = {entry.entry_id for entry in path if entry.conversation_id == mutation.conversation_id}
    return parent if parent in local_ids else None


def validate_entry_owners(entries: Sequence[EntryDraft], path: Sequence[EntryDraft], tasks: Sequence[TaskCheckpoint]) -> None:
    """A late writer cannot append a removed turn onto the new branch."""
    visible = {entry.entry_id for entry in path}
    owners = {entry.by_task_id for entry in entries if entry.by_task_id}
    for task in tasks:
        source = task.state.get("input_entry_id")
        if task.task_id in owners and source and source not in visible:
            raise CommitConflict("task input is outside the active conversation branch")
