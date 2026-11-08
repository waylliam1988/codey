from __future__ import annotations

import hashlib
from collections.abc import Mapping

from codey.durable import conversation_ops
from codey.durable.errors import (
    CommitConflict,
)
from codey.durable.model import (
    CommitResult,
    CreateConversation,
    DurableMutation,
    EntryDraft,
)
from codey.durable.operations import SessionOperations, operation_line


class ConversationOperations(SessionOperations):
    """Own conversation edits and snapshots on the shared transaction line."""

    @operation_line
    def create_conversation(self, conversation_id: str) -> CommitResult:
        if self._kernel.store.has_conversation(conversation_id):
            return CommitResult(self._kernel.store.head(conversation_id)[1], self._kernel.store.commit_sequence())
        return self._kernel._commit(DurableMutation(
            conversation_id=conversation_id,
            expected_revision=0,
            create_conversation=CreateConversation(),
        ))

    @operation_line
    def record_display_message(self, conversation_id: str, *, run_id: str, identity: str,
                               payload: Mapping[str, object]) -> None:
        """Idempotent presentation history; it never enters provider context."""
        entry_id = 'display-' + hashlib.sha256(f'{len(run_id)}:{run_id}{identity}'.encode()).hexdigest()
        if any(entry.entry_id == entry_id for entry in self._kernel.store.entries(conversation_id)):
            return
        # Display observations from a removed turn remain in the execution
        # audit, but must never be appended onto a replacement conversation.
        try:
            owner = self._kernel.store.task(run_id)
        except KeyError:
            owner = None
        if owner is not None and owner.state.get("input_entry_id") and owner.state["input_entry_id"] not in {
            entry.entry_id for entry in conversation_ops.context(self._kernel, conversation_id)
        }:
            return
        head, revision = self._kernel.store.head(conversation_id)
        entry = EntryDraft(entry_id=entry_id, conversation_id=conversation_id, parent_id=head,
                           kind='display_message', by_task_id=run_id,
                           payload={**dict(payload), 'display_only': True})
        self._kernel._commit(DurableMutation(conversation_id, revision, entries=(entry,)))

    @operation_line
    def select_idle_branch(self, conversation_id: str, entry_id: str, *, expected_revision: int) -> CommitResult:
        if any(task.conversation_id == conversation_id for task in self._kernel.store.pending_tasks()):
            raise CommitConflict('Wait for this conversation to finish before switching branches.')
        return conversation_ops.branch(self._kernel, conversation_id, entry_id, expected_revision=expected_revision)

    @operation_line
    def append_message(
        self,
        conversation_id: str,
        kind: str,
        payload: Mapping[str, object],
    ) -> EntryDraft:
        return conversation_ops.append_message(self._kernel, conversation_id, kind, payload)

    @operation_line
    def branch(
        self,
        conversation_id: str,
        entry_id: str,
        *,
        expected_revision: int | None = None,
    ) -> CommitResult:
        return conversation_ops.branch(
            self._kernel, conversation_id, entry_id, expected_revision=expected_revision,
        )

    @operation_line
    def fork(self, conversation_id: str, entry_id: str, *, child_conversation_id: str | None = None) -> str:
        return conversation_ops.fork(self._kernel, conversation_id, entry_id, child_conversation_id=child_conversation_id)

    def delete_conversation(self, conversation_id: str) -> CommitResult:
        """Delete an unreferenced conversation without orphaning a fork."""
        with self._kernel.store.history_lifetime.exclusive(), self._kernel.control_line():
            return conversation_ops.delete_conversation(self._kernel, conversation_id)

    @operation_line
    def save_context_snapshot(
        self, conversation_id: str, payload: Mapping[str, object]
    ) -> EntryDraft:
        """Append the latest handoff facts to the canonical session history.

        Snapshot entries are durable metadata, not provider messages. Keeping
        them in the same append stream makes abrupt-process recovery atomic
        without giving the UI or the provider-window cache ownership of facts.
        """
        return conversation_ops.save_context_snapshot(self._kernel, conversation_id, payload)
