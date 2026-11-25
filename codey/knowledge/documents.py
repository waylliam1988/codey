"""Versioned documents; search and graph tables are transactional projections."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from typing import Protocol, TypedDict

from codey.knowledge.index import KnowledgeIndex
from codey.knowledge.note import KnowledgeNote, now_iso
from codey.types.json_value import require_json_integer, require_json_string


class _DocumentRow(Protocol):
    def __getitem__(self, key: str | int, /) -> object: ...


class NoteRevision(TypedDict):
    revision: int
    recorded_at: str
    reason: str
    deleted: int


def _read_document(row: _DocumentRow) -> tuple[KnowledgeNote, str, int]:
    markdown = require_json_string(row["markdown"], "markdown")
    return (KnowledgeNote.from_markdown(markdown), require_json_string(row["path"], "path"),
            require_json_integer(row["revision"], "revision"))


class KnowledgeDocuments:
    def __init__(self, index: KnowledgeIndex) -> None:
        self.index = index
        with index.transaction() as conn:
            for sql in (
                "CREATE TABLE IF NOT EXISTS knowledge_meta(key TEXT PRIMARY KEY,value TEXT NOT NULL)",
                "CREATE TABLE IF NOT EXISTS note_documents(id TEXT PRIMARY KEY,path TEXT NOT NULL UNIQUE,"
                "revision INTEGER NOT NULL,markdown TEXT NOT NULL)",
                "CREATE TABLE IF NOT EXISTS note_revisions(id TEXT NOT NULL,revision INTEGER NOT NULL,"
                "markdown TEXT,path TEXT NOT NULL,recorded_at TEXT NOT NULL,reason TEXT NOT NULL,"
                "PRIMARY KEY(id,revision))",
                "CREATE TABLE IF NOT EXISTS note_knowledge_refs(note_id TEXT NOT NULL,note_revision INTEGER NOT NULL,"
                "target TEXT NOT NULL,revision INTEGER NOT NULL,PRIMARY KEY(note_id,note_revision,target,revision))",
            ):
                conn.execute(sql)

    def initialized(self) -> bool:
        marker: object = self.index._read_conn().execute(
            "SELECT 1 FROM knowledge_meta WHERE key='documents_initialized'"
        ).fetchone()
        return marker is not None

    def initialize(self, notes: list[tuple[KnowledgeNote, str]]) -> None:
        with self.index.transaction() as conn:
            if self.initialized():
                return
            for note, path in notes:
                self.write(note, path, reason="legacy_import")
            conn.execute("INSERT INTO knowledge_meta VALUES('documents_initialized','1')")

    def get(self, note_id: str, revision: int | None = None) -> tuple[KnowledgeNote, str, int] | None:
        conn = self.index._read_conn()
        if revision is None:
            sql = "SELECT * FROM note_documents WHERE id=?"
            args: tuple[str | int, ...] = (note_id,)
        else:
            sql = "SELECT * FROM note_revisions WHERE id=? AND revision=?"
            args = (note_id, revision)
        row: _DocumentRow | None = conn.execute(sql, args).fetchone()
        if row is None or row["markdown"] is None:
            return None
        return _read_document(row)

    def by_path(self, path: str) -> tuple[KnowledgeNote, str, int] | None:
        row: _DocumentRow | None = self.index._read_conn().execute(
            "SELECT * FROM note_documents WHERE path=?", (path,)).fetchone()
        return _read_document(row) if row is not None else None

    def get_many(self, note_ids: list[str]) -> dict[str, tuple[KnowledgeNote, str, int]]:
        if not note_ids:
            return {}
        rows: list[_DocumentRow] = self.index._read_conn().execute(
            "SELECT * FROM note_documents WHERE id IN (SELECT value FROM json_each(?))",
            (json.dumps(note_ids),)).fetchall()
        found = {require_json_string(row["id"], "id"): _read_document(row) for row in rows}
        unique_ids: dict[str, None] = dict.fromkeys(note_ids)
        return {identity: found[identity] for identity in unique_ids if identity in found}

    def all(self) -> list[tuple[KnowledgeNote, str, int]]:
        rows: list[_DocumentRow] = self.index._read_conn().execute("SELECT * FROM note_documents ORDER BY id").fetchall()
        return [_read_document(row) for row in rows]

    def write(self, note: KnowledgeNote, path: str, *, reason: str = "edit") -> int:
        text = note.to_markdown()
        with self.index.transaction() as conn:
            old: _DocumentRow | None = conn.execute(
                "SELECT markdown,revision,path FROM note_documents WHERE id=?", (note.id,)).fetchone()
            unchanged_text = old is not None and old["markdown"] == text
            if old is not None and unchanged_text and old["path"] == path:
                return require_json_integer(old["revision"], "revision")
            version = self._next_revision(conn, note.id)
            conn.execute("INSERT INTO note_revisions VALUES(?,?,?,?,?,?)", (note.id, version, text, path, now_iso(), reason))
            conn.execute("INSERT INTO note_documents VALUES(?,?,?,?) ON CONFLICT(id) DO UPDATE SET "
                         "path=excluded.path,revision=excluded.revision,markdown=excluded.markdown",
                         (note.id, path, version, text))
            if old is not None and unchanged_text:
                conn.execute("INSERT INTO note_knowledge_refs "
                             "SELECT note_id,?,target,revision FROM note_knowledge_refs WHERE note_id=? AND note_revision=?",
                             (version, note.id, require_json_integer(old["revision"], "revision")))
            self.index.upsert(note, path=path, content_hash=hashlib.sha256(text.encode()).hexdigest()[:16])
            return version

    def remove(self, note_id: str, *, purge: bool = False) -> None:
        with self.index.transaction() as conn:
            old: _DocumentRow | None = conn.execute("SELECT path FROM note_documents WHERE id=?", (note_id,)).fetchone()
            if old is not None:
                conn.execute("INSERT INTO note_revisions VALUES(?,?,?,?,?,?)",
                             (note_id, self._next_revision(conn, note_id), None,
                              require_json_string(old[0], "path"), now_iso(), "compensation"))
            conn.execute("DELETE FROM note_documents WHERE id=?", (note_id,))
            if purge:
                conn.execute("DELETE FROM note_revisions WHERE id=?", (note_id,))
                conn.execute("DELETE FROM note_knowledge_refs WHERE note_id=?", (note_id,))
            self.index.remove(note_id)

    def history(self, note_id: str) -> list[NoteRevision]:
        rows: list[_DocumentRow] = self.index._read_conn().execute(
            "SELECT revision,recorded_at,reason,markdown IS NULL AS deleted FROM note_revisions WHERE id=? ORDER BY revision",
            (note_id,),
        ).fetchall()
        return [NoteRevision(revision=require_json_integer(row["revision"], "revision"),
                             recorded_at=require_json_string(row["recorded_at"], "recorded_at"),
                             reason=require_json_string(row["reason"], "reason"),
                             deleted=require_json_integer(row["deleted"], "deleted")) for row in rows]

    @staticmethod
    def _next_revision(conn: sqlite3.Connection, note_id: str) -> int:
        row: _DocumentRow = conn.execute(
            "SELECT COALESCE(MAX(revision),0)+1 FROM note_revisions WHERE id=?", (note_id,)).fetchone()
        return require_json_integer(row[0], "revision")
