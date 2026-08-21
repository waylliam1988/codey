"""SQLite index over the Markdown knowledge vault.

Markdown files are authoritative. SQLite is a rebuildable cache for search,
recent-note lookup, and relations.
"""

from __future__ import annotations

import json
import sqlite3
import threading
from pathlib import Path

from codey.knowledge.note import KnowledgeNote


class KnowledgeIndex:
    def __init__(self, db_path: Path) -> None:
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(self.db_path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._lock = threading.RLock()
        self.fts_enabled = self._detect_fts()
        self._create_schema()

    def _detect_fts(self) -> bool:
        try:
            self._conn.execute("CREATE VIRTUAL TABLE IF NOT EXISTS _fts_probe USING fts5(x)")
            self._conn.execute("DROP TABLE IF EXISTS _fts_probe")
            return True
        except sqlite3.OperationalError:
            return False

    def _create_schema(self) -> None:
        c = self._conn
        c.executescript(
            """
            CREATE TABLE IF NOT EXISTS notes(
                id TEXT PRIMARY KEY, path TEXT, type TEXT, title TEXT,
                confidence REAL, status TEXT, session_id TEXT, project TEXT,
                created TEXT, updated TEXT, content_hash TEXT, body TEXT,
                open_questions TEXT
            );
            CREATE TABLE IF NOT EXISTS links(
                src_id TEXT, dst_id TEXT, kind TEXT,
                UNIQUE(src_id, dst_id, kind)
            );
            CREATE TABLE IF NOT EXISTS tags(
                note_id TEXT, tag TEXT, UNIQUE(note_id, tag)
            );
            CREATE TABLE IF NOT EXISTS sources(
                note_id TEXT, source TEXT, UNIQUE(note_id, source)
            );
            CREATE TABLE IF NOT EXISTS concept_edges(
                note_id TEXT, src TEXT, dst TEXT, kind TEXT,
                UNIQUE(note_id, src, dst, kind)
            );
            CREATE INDEX IF NOT EXISTS idx_notes_session ON notes(session_id, updated);
            CREATE INDEX IF NOT EXISTS idx_notes_type ON notes(type, updated);
            CREATE INDEX IF NOT EXISTS idx_links_src ON links(src_id);
            CREATE INDEX IF NOT EXISTS idx_links_dst ON links(dst_id);
            CREATE INDEX IF NOT EXISTS idx_tags_tag ON tags(tag);
            CREATE INDEX IF NOT EXISTS idx_concept_src ON concept_edges(src);
            CREATE INDEX IF NOT EXISTS idx_concept_dst ON concept_edges(dst);
            """
        )
        if self.fts_enabled:
            c.execute(
                "CREATE VIRTUAL TABLE IF NOT EXISTS notes_fts USING fts5("
                "id UNINDEXED, title, body, tokenize='unicode61')"
            )
        c.commit()

    def upsert(self, note: KnowledgeNote, *, path: str, content_hash: str) -> None:
        with self._lock:
            c = self._conn
            c.execute(
                "INSERT INTO notes(id,path,type,title,confidence,status,session_id,project,"
                "created,updated,content_hash,body,open_questions) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?) "
                "ON CONFLICT(id) DO UPDATE SET path=excluded.path,type=excluded.type,"
                "title=excluded.title,confidence=excluded.confidence,status=excluded.status,"
                "session_id=excluded.session_id,project=excluded.project,"
                "created=excluded.created,updated=excluded.updated,"
                "content_hash=excluded.content_hash,body=excluded.body,"
                "open_questions=excluded.open_questions",
                (
                    note.id,
                    path,
                    note.type,
                    note.title,
                    note.confidence,
                    note.status,
                    note.session_id,
                    note.project,
                    note.created,
                    note.updated,
                    content_hash,
                    note.body,
                    json.dumps(note.open_questions, ensure_ascii=False, separators=(",", ":")),
                ),
            )
            c.execute("DELETE FROM tags WHERE note_id=?", (note.id,))
            c.executemany(
                "INSERT OR IGNORE INTO tags(note_id,tag) VALUES(?,?)",
                [(note.id, t) for t in note.tags],
            )
            c.execute("DELETE FROM sources WHERE note_id=?", (note.id,))
            c.executemany(
                "INSERT OR IGNORE INTO sources(note_id,source) VALUES(?,?)",
                [(note.id, s) for s in note.sources],
            )
            c.execute("DELETE FROM concept_edges WHERE note_id=?", (note.id,))
            c.executemany(
                "INSERT OR IGNORE INTO concept_edges(note_id,src,dst,kind) VALUES(?,?,?,?)",
                [(note.id, r["src"], r["dst"], r["kind"]) for r in note.relations],
            )
            c.execute("DELETE FROM links WHERE src_id=?", (note.id,))
            if self.fts_enabled:
                c.execute("DELETE FROM notes_fts WHERE id=?", (note.id,))
                c.execute(
                    "INSERT INTO notes_fts(id,title,body) VALUES(?,?,?)",
                    (note.id, note.title, note.body),
                )
            c.commit()

    def remove(self, note_id: str) -> None:
        with self._lock:
            c = self._conn
            c.execute("DELETE FROM links WHERE src_id=? OR dst_id=?", (note_id, note_id))
            c.execute("DELETE FROM tags WHERE note_id=?", (note_id,))
            c.execute("DELETE FROM sources WHERE note_id=?", (note_id,))
            c.execute("DELETE FROM concept_edges WHERE note_id=?", (note_id,))
            c.execute("DELETE FROM notes WHERE id=?", (note_id,))
            if self.fts_enabled:
                c.execute("DELETE FROM notes_fts WHERE id=?", (note_id,))
            c.commit()

    def clear(self) -> None:
        with self._lock:
            c = self._conn
            c.execute("DELETE FROM links")
            c.execute("DELETE FROM tags")
            c.execute("DELETE FROM sources")
            c.execute("DELETE FROM concept_edges")
            c.execute("DELETE FROM notes")
            if self.fts_enabled:
                c.execute("DELETE FROM notes_fts")
            c.commit()

    def add_link(self, src_id: str, dst_id: str, kind: str = "relates") -> None:
        with self._lock:
            self._conn.execute(
                "INSERT OR IGNORE INTO links(src_id,dst_id,kind) VALUES(?,?,?)",
                (src_id, dst_id, kind),
            )
            self._conn.commit()

    def get(self, note_id: str) -> dict | None:
        with self._lock:
            row = self._conn.execute("SELECT * FROM notes WHERE id=?", (note_id,)).fetchone()
        return dict(row) if row else None

    def count(self) -> int:
        with self._lock:
            row = self._conn.execute("SELECT COUNT(*) FROM notes").fetchone()
        return int(row[0])

    def resolve(self, target: str) -> str | None:
        if self.get(target):
            return target
        with self._lock:
            row = self._conn.execute(
                "SELECT id FROM notes WHERE title=? LIMIT 1",
                (target,),
            ).fetchone()
        return row["id"] if row else None

    def search(self, query: str, limit: int = 8) -> list[dict]:
        query = (query or "").strip()
        if not query:
            return []
        with self._lock:
            if self.fts_enabled:
                try:
                    rows = self._conn.execute(
                        "SELECT n.id,n.type,n.title,n.status,n.confidence,n.updated,"
                        "n.session_id,n.project,"
                        " snippet(notes_fts,2,'','','...',12) AS snippet"
                        " FROM notes_fts f JOIN notes n ON n.id=f.id"
                        " WHERE notes_fts MATCH ? ORDER BY rank LIMIT ?",
                        (_fts_query(query), limit),
                    ).fetchall()
                    if rows:
                        return [dict(r) for r in rows]
                except sqlite3.OperationalError:
                    pass
            like = f"%{query}%"
            rows = self._conn.execute(
                "SELECT id,type,title,status,confidence,updated,session_id,project,"
                " substr(body,1,160) AS snippet FROM notes"
                " WHERE title LIKE ? OR body LIKE ? ORDER BY updated DESC LIMIT ?",
                (like, like, limit),
            ).fetchall()
        return [dict(r) for r in rows]

    def recent(
        self,
        limit: int = 20,
        *,
        session_id: str = "",
        types: tuple[str, ...] = (),
        project: str = "",
    ) -> list[dict]:
        clauses: list[str] = []
        args: list[object] = []
        if session_id:
            clauses.append("session_id=?")
            args.append(session_id)
        if project:
            clauses.append("project=?")
            args.append(project)
        if types:
            clauses.append("type IN (" + ",".join("?" * len(types)) + ")")
            args.extend(types)
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        args.append(limit)
        with self._lock:
            rows = self._conn.execute(
                "SELECT id,type,title,status,updated,session_id,project,body,open_questions FROM notes"
                f"{where} ORDER BY updated DESC LIMIT ?",
                tuple(args),
            ).fetchall()
        return [dict(r) for r in rows]

    def links_for(self, note_ids: list[str]) -> list[dict]:
        if not note_ids:
            return []
        marks = ",".join("?" * len(note_ids))
        with self._lock:
            rows = self._conn.execute(
                f"SELECT src_id,dst_id,kind FROM links WHERE src_id IN ({marks})",
                note_ids,
            ).fetchall()
        return [dict(r) for r in rows]

    def notes_by_ids(self, note_ids: list[str]) -> list[dict]:
        ids = _unique(note_ids)
        if not ids:
            return []
        marks = ",".join("?" * len(ids))
        with self._lock:
            rows = self._conn.execute(
                "SELECT id,path,type,title,confidence,status,session_id,project,"
                "created,updated,content_hash,body,open_questions FROM notes"
                f" WHERE id IN ({marks})",
                ids,
            ).fetchall()
        by_id = {str(row["id"]): dict(row) for row in rows}
        return [by_id[note_id] for note_id in ids if note_id in by_id]

    def links_touching(self, note_ids: list[str]) -> list[dict]:
        ids = _unique(note_ids)
        if not ids:
            return []
        marks = ",".join("?" * len(ids))
        with self._lock:
            rows = self._conn.execute(
                "SELECT src_id,dst_id,kind FROM links"
                f" WHERE src_id IN ({marks}) OR dst_id IN ({marks})",
                (*ids, *ids),
            ).fetchall()
        return [dict(r) for r in rows]

    def replace_links_touching(self, note_ids: list[str], links: list[dict]) -> None:
        ids = _unique(note_ids)
        if not ids:
            return
        marks = ",".join("?" * len(ids))
        rows = [
            (
                str(row.get("src_id") or ""),
                str(row.get("dst_id") or ""),
                str(row.get("kind") or "relates"),
            )
            for row in links
            if str(row.get("src_id") or "") and str(row.get("dst_id") or "")
        ]
        with self._lock:
            c = self._conn
            c.execute(
                "DELETE FROM links"
                f" WHERE src_id IN ({marks}) OR dst_id IN ({marks})",
                (*ids, *ids),
            )
            c.executemany(
                "INSERT OR IGNORE INTO links(src_id,dst_id,kind) VALUES(?,?,?)",
                rows,
            )
            c.commit()

    def sources_for(self, note_ids: list[str]) -> list[dict]:
        ids = _unique(note_ids)
        if not ids:
            return []
        marks = ",".join("?" * len(ids))
        with self._lock:
            rows = self._conn.execute(
                f"SELECT note_id,source FROM sources WHERE note_id IN ({marks})",
                ids,
            ).fetchall()
        return [dict(r) for r in rows]

    def tags_for(self, note_ids: list[str], *, active_only: bool = False) -> list[dict]:
        ids = _unique(note_ids)
        if not ids:
            return []
        marks = ",".join("?" * len(ids))
        with self._lock:
            if active_only:
                rows = self._conn.execute(
                    "SELECT t.note_id,t.tag FROM tags t"
                    " JOIN notes n ON n.id=t.note_id"
                    f" WHERE n.status='active' AND t.note_id IN ({marks})",
                    ids,
                ).fetchall()
            else:
                rows = self._conn.execute(
                    f"SELECT note_id,tag FROM tags WHERE note_id IN ({marks})",
                    ids,
                ).fetchall()
        return [dict(r) for r in rows]

    def concept_edge_rows(self, limit: int = 2048, *, session_id: str = "") -> list[dict]:
        """Declared concept relations from active notes, newest first.

        When a session is requested, rows from that session are read before the
        global backfill so older target-session relations cannot be truncated by
        newer unrelated vault activity.
        """
        limit = max(0, int(limit or 0))
        if limit <= 0:
            return []
        select_sql = (
            "SELECT e.note_id,e.src,e.dst,e.kind,n.session_id,n.project,n.title FROM concept_edges e"
            " JOIN notes n ON n.id=e.note_id WHERE n.status='active'"
        )
        session_id = str(session_id or "").strip()
        with self._lock:
            rows = []
            if session_id:
                rows.extend(
                    self._conn.execute(
                        select_sql + " AND n.session_id=? ORDER BY n.updated DESC LIMIT ?",
                        (session_id, limit),
                    ).fetchall()
                )
            if len(rows) < limit:
                rows.extend(
                    self._conn.execute(
                        select_sql + " ORDER BY n.updated DESC LIMIT ?",
                        (limit,),
                    ).fetchall()
                )
        return _unique_rows([dict(r) for r in rows], limit, ("note_id", "src", "dst", "kind"))

    def tag_concept_rows(self, limit: int = 4096, *, session_id: str = "") -> list[dict]:
        """Raw tag rows of active notes joined with note metadata.

        Session rows are read first for the same reason as `concept_edge_rows`:
        an old Research run should still be diagnosable after the vault grows.
        """
        limit = max(0, int(limit or 0))
        if limit <= 0:
            return []
        select_sql = (
            "SELECT t.note_id,t.tag,n.type,n.title,n.session_id,n.project,n.updated"
            " FROM tags t JOIN notes n ON n.id=t.note_id"
            " WHERE n.status='active'"
        )
        session_id = str(session_id or "").strip()
        with self._lock:
            rows = []
            if session_id:
                rows.extend(
                    self._conn.execute(
                        select_sql + " AND n.session_id=? ORDER BY n.updated DESC LIMIT ?",
                        (session_id, limit),
                    ).fetchall()
                )
            if len(rows) < limit:
                rows.extend(
                    self._conn.execute(
                        select_sql + " ORDER BY n.updated DESC LIMIT ?",
                        (limit,),
                    ).fetchall()
                )
        return _unique_rows([dict(r) for r in rows], limit, ("note_id", "tag"))

    def close(self) -> None:
        with self._lock:
            self._conn.close()


def _fts_query(query: str) -> str:
    terms = [t for t in _tokenize(query) if t]
    if not terms:
        return '""'
    return " OR ".join(f'"{t}"*' for t in terms[:8])


def _tokenize(query: str) -> list[str]:
    out: list[str] = []
    current: list[str] = []
    for ch in query.lower():
        if ch.isalnum() or "\u4e00" <= ch <= "\u9fff":
            current.append(ch)
        elif current:
            out.append("".join(current))
            current = []
    if current:
        out.append("".join(current))
    return out


def _unique(values: list[str]) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for value in values:
        item = str(value or "").strip()
        if item and item not in seen:
            seen.add(item)
            out.append(item)
    return out


def _unique_rows(rows: list[dict], limit: int, keys: tuple[str, ...]) -> list[dict]:
    out: list[dict] = []
    seen: set[tuple[str, ...]] = set()
    for row in rows:
        key = tuple(str(row.get(item) or "") for item in keys)
        if key in seen:
            continue
        seen.add(key)
        out.append(row)
        if len(out) >= limit:
            break
    return out
