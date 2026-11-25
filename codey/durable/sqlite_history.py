"""Indexed history path reads on a caller-owned SQLite read transaction."""

from __future__ import annotations

import sqlite3
from collections.abc import Iterable, Mapping

from codey.durable.history import HistoryEntry
from codey.durable.model import ArtifactRecord, EntryDraft, TaskCheckpoint
from codey.durable.sqlite_display import SQLiteDisplayIndex
from codey.durable.sqlite_records import (
    _column,
    _integer,
    _optional_text,
    _text,
    artifact_from_row,
    entry_from_row,
    task_from_row,
)


def _history_entry(row: sqlite3.Row, payload: Mapping[str, object]) -> HistoryEntry:
    return HistoryEntry(
        _text(row, "id"), _text(row, "conversation_id"), _optional_text(row, "parent_id"),
        _text(row, "kind"), _optional_text(row, "by_task_id"), payload,
    )


class SQLiteHistoryReader(SQLiteDisplayIndex):
    def display_head(self, conversation_id: str) -> tuple[str | None, int, int]:
        row: sqlite3.Row | None = self.database.execute(
            "SELECT head_entry_id, parent_entry_id, revision, path_generation FROM conversations WHERE id=?",
            (conversation_id,),
        ).fetchone()
        if row is None:
            raise KeyError(f"unknown conversation: {conversation_id}")
        return (
            _optional_text(row, "head_entry_id") or _optional_text(row, "parent_entry_id"),
            _integer(row, "revision"),
            _integer(row, "path_generation"),
        )

    def cursor_key(self) -> bytes:
        row: sqlite3.Row | None = self.database.execute(
            "SELECT value FROM schema_metadata WHERE key='history_cursor_key'",
        ).fetchone()
        if row is None:
            raise ValueError("missing history cursor key")
        return _text(row, "value").encode("ascii")

    def navigation_items(self, identities: list[str]) -> list[dict[str, object]]:
        if not identities:
            return []
        placeholders = ",".join("?" for _ in identities)
        rows: list[sqlite3.Row] = self.database.execute(
            f"SELECT entry_id, question_text FROM display_versions WHERE entry_id IN ({placeholders})", identities,
        ).fetchall()
        texts = {_text(row, "entry_id"): _text(row, "question_text") for row in rows}
        return [{"id": identity, "text": texts[identity]} for identity in identities]

    def display_entries(self, identities: list[str]) -> tuple[HistoryEntry, ...]:
        if not identities:
            return ()
        placeholders = ",".join("?" for _ in identities)
        rows: list[sqlite3.Row] = self.database.execute(
            f"SELECT id, conversation_id, parent_id, kind, by_task_id FROM entries WHERE id IN ({placeholders})",
            identities,
        ).fetchall()
        entries = {entry.entry_id: entry for entry in (_history_entry(row, {}) for row in rows)}
        return tuple(entries[identity] for identity in identities)

    def head(self, conversation_id: str) -> tuple[str | None, int]:
        row: sqlite3.Row | None = self.database.execute(
            "SELECT head_entry_id, revision FROM conversations WHERE id=?", (conversation_id,),
        ).fetchone()
        if row is None:
            raise KeyError(f"unknown conversation: {conversation_id}")
        return _optional_text(row, "head_entry_id"), _integer(row, "revision")

    def conversation_parent(self, conversation_id: str) -> tuple[str | None, str | None]:
        row: sqlite3.Row | None = self.database.execute(
            "SELECT parent_conversation_id, parent_entry_id FROM conversations WHERE id=?", (conversation_id,),
        ).fetchone()
        if row is None:
            raise KeyError(f"unknown conversation: {conversation_id}")
        return _optional_text(row, "parent_conversation_id"), _optional_text(row, "parent_entry_id")

    def local_path(self, conversation_id: str, leaf_id: str) -> tuple[HistoryEntry, ...]:
        # UNION also stops cycles in damaged histories; the validation below
        # rejects them instead of allowing an unbounded recursive query.
        rows: list[sqlite3.Row] = self.database.execute(
            "WITH RECURSIVE path(id, parent_id) AS ("
            " SELECT id, parent_id FROM entries WHERE conversation_id=? AND id=?"
            " UNION SELECT e.id, e.parent_id FROM entries e JOIN path p ON e.id=p.parent_id"
            " WHERE e.conversation_id=?"
            ") SELECT e.id, e.conversation_id, e.parent_id, e.kind, e.by_task_id,"
            " json_type(e.payload, '$.head_marker')='true' AS head_marker,"
            " json_extract(e.payload, '$.edited_entry_id') AS edited_entry_id,"
            " json_extract(e.payload, '$.edit_parent_entry_id') AS edit_parent_entry_id"
            " FROM path JOIN entries e ON e.id=path.id ORDER BY e.ordinal",
            (conversation_id, leaf_id, conversation_id),
        ).fetchall()
        path = tuple(_history_entry(row, {
            "head_marker": _column(row, "head_marker") == 1,
            "edited_entry_id": _column(row, "edited_entry_id"),
            "edit_parent_entry_id": _column(row, "edit_parent_entry_id"),
        }) for row in rows)
        parent_id = None
        for entry in path:
            if entry.parent_id != parent_id:
                raise ValueError("invalid conversation entry path")
            parent_id = entry.entry_id
        return path

    def run_ids(self, conversation_id: str) -> set[str]:
        rows: Iterable[sqlite3.Row] = self.database.execute(
            "SELECT DISTINCT by_task_id FROM entries WHERE conversation_id=? AND by_task_id IS NOT NULL",
            (conversation_id,),
        )
        return {run for row in rows if (run := _text(row, "by_task_id"))}

    def select_entries(self, selected: tuple[HistoryEntry, ...]) -> tuple[EntryDraft, ...]:
        if not selected:
            return ()
        placeholders = ",".join("?" for _ in selected)
        rows: list[sqlite3.Row] = self.database.execute(
            f"SELECT * FROM entries WHERE id IN ({placeholders})", tuple(entry.entry_id for entry in selected),
        ).fetchall()
        entries = {_text(row, "id"): entry_from_row(row) for row in rows}
        return tuple(entries[entry.entry_id] for entry in selected)

    def task(self, task_id: str) -> TaskCheckpoint:
        row: sqlite3.Row | None = self.database.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
        if row is None:
            raise KeyError(f"unknown task: {task_id}")
        return task_from_row(row)

    def artifact(self, handle: str) -> ArtifactRecord:
        row: sqlite3.Row | None = self.database.execute("SELECT * FROM artifacts WHERE handle=?", (handle,)).fetchone()
        if row is None:
            raise KeyError(f"unknown artifact: {handle}")
        return artifact_from_row(row)
