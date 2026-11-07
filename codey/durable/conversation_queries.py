"""Read-only conversation and history facade for one durable session.

The facade owns conversation query algorithms while ``DurableSession`` keeps
the single mutation line and commit kernel.  Every public method executes
through the callback supplied by that kernel, so a read/derive operation
cannot observe a writer between its reads.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import replace

from codey.durable import conversation_ops
from codey.durable.context import ContextSnapshot
from codey.durable.inbox import InboxDoc, InboxHistoryEvent, history_event_from_projection, item_from_record
from codey.durable.model import EntryDraft, SubmissionRecord
from codey.durable.query_line import QueryLine
from codey.durable.session_kernel import SessionKernel


class ConversationQueryFacade:
    """Own conversation, history, inbox, and context reads."""

    def __init__(self, session: SessionKernel, query_line: QueryLine) -> None:
        self._session = session
        self._query_line = query_line

    def conversation_ids(self) -> tuple[str, ...]:
        return self._query_line(lambda: self._session.store.conversation_ids())

    def head(self, conversation_id: str) -> tuple[str | None, int]:
        def read() -> tuple[str | None, int]:
            head, revision = self._session.store.head(conversation_id)
            return str(head) if head is not None else None, int(revision)

        return self._query_line(read)

    def has_conversation(self, conversation_id: str) -> bool:
        def read() -> bool:
            return self._session.store.has_conversation(conversation_id)

        return self._query_line(read)

    def navigation_page(
        self,
        conversation_id: str,
        *,
        before: str | None = None,
        limit: int = 200,
    ) -> dict[str, object]:
        return self._query_line(
            lambda: conversation_ops.navigation_page(
                self._session, conversation_id, before=before, limit=limit,
            )
        )

    def history_window(
        self,
        conversation_id: str,
        entry_id: str,
        *,
        limit: int = 100,
        leaf_id: str | None = None,
    ) -> dict[str, object]:
        return self._query_line(
            lambda: conversation_ops.history_window(
                self._session, conversation_id, entry_id, limit=limit, leaf_id=leaf_id,
            )
        )

    def conversation_tree(self, conversation_id: str) -> dict[str, object]:
        return self._query_line(lambda: conversation_ops.conversation_tree(self._session, conversation_id))

    def conversation_lineage(self, conversation_id: str) -> list[dict[str, object]]:
        return self._query_line(lambda: conversation_ops.conversation_lineage(self._session, conversation_id))

    def queued_submissions(self, conversation_id: str | None = None) -> tuple[SubmissionRecord, ...]:
        return self._query_line(lambda: tuple(self._session.store.queued_submissions(conversation_id)))

    def inbox_doc(self, conversation_id: str) -> InboxDoc:
        def read() -> InboxDoc:
            _head, revision = self._session.store.head(conversation_id)
            tasks = {
                task.submission_id: task.task_id
                for task in self._session.store.tasks()
                if task.conversation_id == conversation_id
            }
            items = tuple(
                item_from_record(record, task_id=tasks.get(record.submission_id))
                for record in self._session.store.queued_submissions(conversation_id)
            )
            return InboxDoc(conversation_id, revision, items)

        return self._query_line(read)

    def inbox_history(self, conversation_id: str) -> tuple[InboxHistoryEvent, ...]:
        def read() -> tuple[InboxHistoryEvent, ...]:
            events: list[InboxHistoryEvent] = []
            cursor = 0
            while True:
                records = self._session.store.commits_after(cursor, limit=512)
                if not records:
                    break
                for record in records:
                    cursor = max(cursor, record.sequence)
                    if record.payload.get("session_id") != conversation_id:
                        continue
                    projections = record.payload.get("projections", ())
                    if not isinstance(projections, (list, tuple)):
                        continue
                    for projection in projections:
                        if not isinstance(projection, Mapping):
                            continue
                        event = history_event_from_projection(record.sequence, conversation_id, projection)
                        if event is not None:
                            if not event.request_id:
                                try:
                                    request_id = self._session.store.submission(event.submission_id).request_id
                                except KeyError:
                                    request_id = ""
                                event = replace(event, request_id=request_id)
                            events.append(event)
                if len(records) < 512:
                    break
            return tuple(events)

        return self._query_line(read)

    def entries(self, conversation_id: str) -> tuple[EntryDraft, ...]:
        return self._query_line(lambda: conversation_ops.entries(self._session, conversation_id))

    def history_page(
        self,
        conversation_id: str,
        *,
        before: str | None = None,
        limit: int = 100,
    ) -> dict[str, object]:
        return self._query_line(
            lambda: conversation_ops.history_page(
                self._session, conversation_id, before=before, limit=limit,
            )
        )

    def search_history(
        self,
        conversation_id: str,
        query: str,
        *,
        limit: int = 50,
    ) -> list[dict[str, object]]:
        return self._query_line(
            lambda: conversation_ops.search_history(
                self._session, conversation_id, query, limit=limit,
            )
        )

    def context(self, conversation_id: str, *, leaf_id: str | None = None) -> tuple[EntryDraft, ...]:
        return self._query_line(
            lambda: conversation_ops.context(self._session, conversation_id, leaf_id=leaf_id)
        )

    def context_at(
        self,
        conversation_id: str,
        commit_seq: int,
        *,
        leaf_id: str | None = None,
    ) -> tuple[EntryDraft, ...]:
        return self._query_line(
            lambda: conversation_ops.context_at(
                self._session, conversation_id, commit_seq, leaf_id=leaf_id,
            )
        )

    def context_snapshot(
        self,
        conversation_id: str,
        *,
        token_budget: int,
        leaf_id: str | None = None,
        before_entry_id: str | None = None,
        token_counter: Callable[[tuple[Mapping[str, object], ...]], int] | None = None,
    ) -> ContextSnapshot:
        return self._query_line(
            lambda: conversation_ops.context_snapshot(
                self._session,
                conversation_id,
                token_budget=token_budget,
                leaf_id=leaf_id,
                before_entry_id=before_entry_id,
                token_counter=token_counter,
            )
        )

    def context_snapshot_at(
        self,
        conversation_id: str,
        commit_seq: int,
        *,
        token_budget: int,
        leaf_id: str | None = None,
        token_counter: Callable[[tuple[Mapping[str, object], ...]], int] | None = None,
    ) -> ContextSnapshot:
        return self._query_line(
            lambda: conversation_ops.context_snapshot_at(
                self._session,
                conversation_id,
                commit_seq,
                token_budget=token_budget,
                leaf_id=leaf_id,
                token_counter=token_counter,
            )
        )

    def latest_context_snapshot(self, conversation_id: str) -> EntryDraft | None:
        return self._query_line(
            lambda: conversation_ops.latest_context_snapshot(self._session, conversation_id)
        )


__all__ = ["ConversationQueryFacade", "QueryLine"]
