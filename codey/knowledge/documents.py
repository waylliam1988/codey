"""Versioned documents; search and graph tables are transactional projections."""

from __future__ import annotations

import hashlib
import sqlite3
from typing import Any

from codey.knowledge.index import KnowledgeIndex
from codey.knowledge.note import KnowledgeNote, now_iso


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
            ):
                conn.execute(sql)

    def initialized(self) -> bool:
        return self.index._read_conn().execute(
            "SELECT 1 FROM knowledge_meta WHERE key='documents_initialized'"
        ).fetchone() is not None

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
            row = conn.execute("SELECT * FROM note_documents WHERE id=?", (note_id,)).fetchone()
        else:
            row = conn.execute("SELECT * FROM note_revisions WHERE id=? AND revision=?", (note_id, revision)).fetchone()
        if row is None or row["markdown"] is None:
            return None
        return KnowledgeNote.from_markdown(row["markdown"]), str(row["path"]), int(row["revision"])

    def by_path(self, path: str) -> tuple[KnowledgeNote, str, int] | None:
        row = self.index._read_conn().execute("SELECT id FROM note_documents WHERE path=?", (path,)).fetchone()
        return self.get(str(row[0])) if row else None

    def all(self) -> list[tuple[KnowledgeNote, str, int]]:
        rows = self.index._read_conn().execute("SELECT * FROM note_documents ORDER BY id").fetchall()
        return [(KnowledgeNote.from_markdown(row["markdown"]), str(row["path"]), int(row["revision"])) for row in rows]

    def write(self, note: KnowledgeNote, path: str, *, reason: str = "edit") -> int:
        text = note.to_markdown()
        with self.index.transaction() as conn:
            old = conn.execute("SELECT markdown,revision FROM note_documents WHERE id=?", (note.id,)).fetchone()
            if old and old["markdown"] == text:
                return int(old["revision"])
            version = self._next_revision(conn, note.id)
            conn.execute("INSERT INTO note_revisions VALUES(?,?,?,?,?,?)", (note.id, version, text, path, now_iso(), reason))
            conn.execute("INSERT INTO note_documents VALUES(?,?,?,?) ON CONFLICT(id) DO UPDATE SET "
                         "path=excluded.path,revision=excluded.revision,markdown=excluded.markdown",
                         (note.id, path, version, text))
            self.index.upsert(note, path=path, content_hash=hashlib.sha256(text.encode()).hexdigest()[:16])
            return version

    def remove(self, note_id: str, *, purge: bool = False) -> None:
        with self.index.transaction() as conn:
            old = conn.execute("SELECT path FROM note_documents WHERE id=?", (note_id,)).fetchone()
            if old:
                conn.execute("INSERT INTO note_revisions VALUES(?,?,?,?,?,?)",
                             (note_id, self._next_revision(conn, note_id), None, old[0], now_iso(), "compensation"))
            conn.execute("DELETE FROM note_documents WHERE id=?", (note_id,))
            if purge:
                conn.execute("DELETE FROM note_revisions WHERE id=?", (note_id,))
            self.index.remove(note_id)

    def history(self, note_id: str) -> list[dict[str, Any]]:
        return [dict(row) for row in self.index._read_conn().execute(
            "SELECT revision,recorded_at,reason,markdown IS NULL AS deleted FROM note_revisions WHERE id=? ORDER BY revision",
            (note_id,),
        )]

    @staticmethod
    def _next_revision(conn: sqlite3.Connection, note_id: str) -> int:
        row = conn.execute("SELECT COALESCE(MAX(revision),0)+1 FROM note_revisions WHERE id=?", (note_id,)).fetchone()
        return int(row[0])
