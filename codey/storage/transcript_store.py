"""Durable, display-only conversation transcript archive.

The archive is intentionally separate from model protocol history and UI state.
It owns stable message identities, keyset pagination, literal search and a
clear epoch so late workers cannot resurrect deleted messages.
"""
from __future__ import annotations

import base64
import json
import secrets
import sqlite3
from pathlib import Path
from typing import Any, cast

from codey.storage.conversation_db import conversation_database

SCHEMA = """
CREATE TABLE IF NOT EXISTS transcript_sessions (
  session_id TEXT PRIMARY KEY,
  epoch TEXT NOT NULL,
  next_ordinal INTEGER NOT NULL,
  revision INTEGER NOT NULL,
  cleared_at TEXT
);
CREATE TABLE IF NOT EXISTS transcript_messages (
  session_id TEXT NOT NULL,
  epoch TEXT NOT NULL,
  message_id TEXT NOT NULL,
  ordinal INTEGER NOT NULL,
  revision INTEGER NOT NULL,
  turn_id TEXT,
  kind TEXT NOT NULL,
  payload TEXT NOT NULL,
  PRIMARY KEY(session_id, epoch, message_id),
  UNIQUE(session_id, epoch, ordinal)
);
CREATE TABLE IF NOT EXISTS transcript_events (
  session_id TEXT NOT NULL,
  epoch TEXT NOT NULL,
  event_id TEXT NOT NULL,
  message_id TEXT NOT NULL,
  PRIMARY KEY(session_id, epoch, event_id)
);
CREATE TABLE IF NOT EXISTS transcript_tombstones (
  session_id TEXT NOT NULL,
  identity TEXT NOT NULL,
  PRIMARY KEY(session_id, identity)
);
CREATE TABLE IF NOT EXISTS transcript_ui_barriers (
  session_id TEXT PRIMARY KEY,
  revision INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS transcript_messages_order ON transcript_messages(session_id, epoch, ordinal);
"""


class TranscriptStore:
    def __init__(self, root: str | Path) -> None:
        self.path = Path(root) / "transcript.sqlite3"
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with conversation_database(self.path) as database:
            database.execute("PRAGMA journal_mode=WAL")
            database.executescript(SCHEMA)

    def close(self) -> None:
        if not self.path.exists():
            return
        with conversation_database(self.path) as database:
            database.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            database.execute("PRAGMA journal_mode=DELETE")

    def _session(self, database: sqlite3.Connection, session_id: str) -> tuple[str, int, int]:
        row = database.execute("SELECT epoch,next_ordinal,revision FROM transcript_sessions WHERE session_id=?", (session_id,)).fetchone()
        if row is None:
            epoch = secrets.token_hex(16)
            database.execute(
                "INSERT OR IGNORE INTO transcript_sessions(session_id,epoch,next_ordinal,revision) VALUES(?,?,?,?)",
                (session_id, epoch, 1, 0),
            )
            row = database.execute(
                "SELECT epoch,next_ordinal,revision FROM transcript_sessions WHERE session_id=?",
                (session_id,),
            ).fetchone()
            assert row is not None
        return str(row[0]), int(row[1]), int(row[2])

    def epoch(self, session_id: str) -> str:
        with conversation_database(self.path) as database:
            return self._session(database, session_id)[0]

    def ui_save_allowed(self, session_id: str, base_revision: int) -> bool:
        """Reject snapshots created before the session was cleared."""
        with conversation_database(self.path) as database:
            row = database.execute(
                "SELECT revision FROM transcript_ui_barriers WHERE session_id=?",
                (session_id,),
            ).fetchone()
            return row is None or int(base_revision) > int(row[0])

    def append(
        self,
        session_id: str,
        payload: dict[str, Any],
        *,
        message_id: str,
        event_id: str = "",
        epoch: str | None = None,
        turn_id: str = "",
    ) -> bool:
        if not session_id or not message_id or not isinstance(payload, dict):
            raise ValueError("invalid transcript message")
        with conversation_database(self.path) as database:
            # Serialize ordinal allocation and epoch checks across writers.
            database.execute("BEGIN IMMEDIATE")
            current_epoch, next_ordinal, current_revision = self._session(database, session_id)
            if epoch is not None and epoch != current_epoch:
                return False
            if database.execute(
                "SELECT 1 FROM transcript_tombstones WHERE session_id=? AND identity IN (?, ?)",
                (session_id, message_id, event_id),
            ).fetchone():
                return False
            if event_id and database.execute("SELECT 1 FROM transcript_events WHERE session_id=? AND epoch=? AND event_id=?", (session_id, current_epoch, event_id)).fetchone():
                return True
            existing = database.execute("SELECT ordinal,revision FROM transcript_messages WHERE session_id=? AND epoch=? AND message_id=?", (session_id, current_epoch, message_id)).fetchone()
            revision = current_revision + 1
            if existing is None:
                ordinal = next_ordinal
                database.execute("INSERT INTO transcript_messages(session_id,epoch,message_id,ordinal,revision,turn_id,kind,payload) VALUES(?,?,?,?,?,?,?,?)", (session_id, current_epoch, message_id, ordinal, revision, str(turn_id or payload.get("turnId") or ""), str(payload.get("type") or "message"), json.dumps(payload, ensure_ascii=False, separators=(",", ":"))))
                next_ordinal += 1
            else:
                ordinal = int(existing[0])
                database.execute("UPDATE transcript_messages SET revision=?,turn_id=?,kind=?,payload=? WHERE session_id=? AND epoch=? AND message_id=?", (revision, str(turn_id or payload.get("turnId") or ""), str(payload.get("type") or "message"), json.dumps(payload, ensure_ascii=False, separators=(",", ":")), session_id, current_epoch, message_id))
            if event_id:
                database.execute("INSERT OR IGNORE INTO transcript_events(session_id,epoch,event_id,message_id) VALUES(?,?,?,?)", (session_id, current_epoch, event_id, message_id))
            database.execute("UPDATE transcript_sessions SET next_ordinal=?,revision=? WHERE session_id=?", (next_ordinal, revision, session_id))
            return True

    def page(self, session_id: str, *, before: str | None = None, limit: int = 100) -> dict[str, Any]:
        limit = max(1, min(200, int(limit)))
        with conversation_database(self.path) as database:
            epoch, next_ordinal, revision = self._session(database, session_id)
            boundary = next_ordinal
            if before:
                decoded = self._decode_cursor(before)
                if decoded[0] != session_id or decoded[1] != epoch:
                    raise ValueError("invalid transcript cursor")
                boundary = decoded[2]
            rows = database.execute("SELECT message_id,ordinal,revision,turn_id,payload FROM transcript_messages WHERE session_id=? AND epoch=? AND ordinal<? ORDER BY ordinal DESC LIMIT ?", (session_id, epoch, boundary, limit)).fetchall()
            rows = list(reversed(rows))
            items = [self._row(item) for item in rows]
            earliest = int(rows[0][1]) if rows else boundary
            more = database.execute("SELECT 1 FROM transcript_messages WHERE session_id=? AND epoch=? AND ordinal<? LIMIT 1", (session_id, epoch, earliest)).fetchone() is not None
            cursor = self._encode_cursor(session_id, epoch, earliest) if more and rows else None
            return {"schema_version": 1, "session_id": session_id, "epoch": epoch, "snapshot_revision": revision, "items": items, "before_cursor": cursor, "has_more": more}

    def search(self, session_id: str, query: str, *, limit: int = 50) -> list[dict[str, Any]]:
        text = query.strip()
        if not text:
            return []
        limit = max(1, min(100, int(limit)))
        with conversation_database(self.path) as database:
            epoch, _next, _revision = self._session(database, session_id)
            rows = database.execute("SELECT message_id,ordinal,revision,turn_id,payload FROM transcript_messages WHERE session_id=? AND epoch=? ORDER BY ordinal ASC", (session_id, epoch)).fetchall()
            hits: list[dict[str, Any]] = []
            for row in rows:
                payload = json.loads(row[4])
                body = str(payload.get("text") or payload.get("result") or payload.get("output") or "")
                index = body.casefold().find(text.casefold())
                if index < 0:
                    continue
                hits.append({"message_id": row[0], "ordinal": row[1], "revision": row[2], "turn_id": row[3], "excerpt": body[max(0, index - 80): index + len(text) + 80]})
                if len(hits) >= limit:
                    break
            return hits

    def clear(self, session_id: str, *, ui_revision: int = 0) -> str:
        with conversation_database(self.path) as database:
            old = self._session(database, session_id)[0]
            identities = database.execute(
                "SELECT message_id FROM transcript_messages WHERE session_id=? AND epoch=? "
                "UNION SELECT event_id FROM transcript_events WHERE session_id=? AND epoch=?",
                (session_id, old, session_id, old),
            ).fetchall()
            database.executemany(
                "INSERT OR IGNORE INTO transcript_tombstones(session_id,identity) VALUES(?,?)",
                ((session_id, str(row[0])) for row in identities if row[0]),
            )
            epoch = secrets.token_hex(16)
            database.execute("DELETE FROM transcript_messages WHERE session_id=? AND epoch=?", (session_id, old))
            database.execute("DELETE FROM transcript_events WHERE session_id=? AND epoch=?", (session_id, old))
            database.execute("UPDATE transcript_sessions SET epoch=?,next_ordinal=1,revision=0,cleared_at=datetime('now') WHERE session_id=?", (epoch, session_id))
            database.execute(
                "INSERT INTO transcript_ui_barriers(session_id,revision) VALUES(?,?) "
                "ON CONFLICT(session_id) DO UPDATE SET revision=excluded.revision",
                (session_id, max(0, int(ui_revision))),
            )
            return epoch

    @staticmethod
    def _row(row: tuple[Any, ...]) -> dict[str, Any]:
        payload = cast(dict[str, Any], json.loads(row[4]))
        payload.update({"id": row[0], "ordinal": row[1], "message_revision": row[2], **({"turnId": row[3]} if row[3] else {})})
        return payload

    @staticmethod
    def _encode_cursor(session_id: str, epoch: str, ordinal: int) -> str:
        raw = json.dumps({"s": session_id, "e": epoch, "o": ordinal}, separators=(",", ":")).encode()
        return base64.urlsafe_b64encode(raw).decode().rstrip("=")

    @staticmethod
    def _decode_cursor(value: str) -> tuple[str, str, int]:
        try:
            raw = base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))
            payload = json.loads(raw)
            if not isinstance(payload, dict) or not isinstance(payload.get("s"), str) or not isinstance(payload.get("e"), str) or type(payload.get("o")) is not int:
                raise ValueError
            return payload["s"], payload["e"], payload["o"]
        except Exception as exc:
            raise ValueError("invalid transcript cursor") from exc
