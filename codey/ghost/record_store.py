"""Ghost candidate, continuity, queue and sleep records with their event cursors."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from collections.abc import Generator, Iterable, Mapping
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Literal

from codey.ghost.payload_types import decoded_object
from codey.ghost.sqlite import GhostSQLite


def _encode(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _payload(value: object) -> dict[str, object]:
    if not isinstance(value, Mapping):
        raise ValueError("Ghost record payload must be an object")
    fields: Mapping[object, object] = value
    result: dict[str, object] = {}
    for key, item in fields.items():
        if not isinstance(key, str):
            raise ValueError("Ghost record keys must be strings")
        result[key] = item
    return result


class GhostRecords:
    def __init__(
        self, database: GhostSQLite, collection: Literal["inbox", "continuity", "work_queue", "sleep"]
    ) -> None:
        self.database = database
        self.collection = collection
        self._connection: ContextVar[sqlite3.Connection | None] = ContextVar("ghost_record_transaction", default=None)

    @contextmanager
    def transaction(self, *, write: bool = True, deleting: bool = False) -> Generator[None]:
        if deleting and not write:
            raise ValueError("deletion requires a write transaction")
        if self._connection.get() is not None:
            if write:
                self.database.check_projection_inputs(self._db())
            if deleting:
                self.database.advance_deletion(self._db())
            yield
            return
        with self.database.connection() as connection:
            connection.execute("BEGIN IMMEDIATE" if write else "BEGIN")
            if write:
                self.database.check_projection_inputs(connection)
            if deleting:
                self.database.advance_deletion(connection)
            token = self._connection.set(connection)
            try:
                yield
                if write:
                    self._check_cursor()
            finally:
                self._connection.reset(token)

    def _db(self) -> sqlite3.Connection:
        connection = self._connection.get()
        if connection is None:
            raise RuntimeError("Ghost record access requires its transaction")
        return connection

    def events(self) -> list[dict[str, object]]:
        with self.transaction(write=False):
            rows: Iterable[sqlite3.Row] = self._db().execute(
                "SELECT payload FROM memory_events WHERE collection=? ORDER BY sequence", (self.collection,),
            )
            return [decoded_object(row[0]) for row in rows]

    def rows(self) -> list[dict[str, object]]:
        with self.transaction(write=False):
            self._check_cursor()
            rows: Iterable[sqlite3.Row] = self._db().execute(
                "SELECT payload FROM memory_records WHERE collection=? ORDER BY id", (self.collection,),
            )
            return [decoded_object(row[0]) for row in rows]

    def append(self, events: Iterable[Mapping[str, object]], *, replace: bool = False) -> None:
        with self.transaction():
            database = self._db()
            if replace:
                database.execute("DELETE FROM memory_events WHERE collection=?", (self.collection,))
            for event in events:
                payload = _encode(event)
                identity = str(event.get("event_id") or hashlib.sha256(payload.encode()).hexdigest())
                old: sqlite3.Row | None = database.execute(
                    "SELECT payload FROM memory_events WHERE collection=? AND event_id=?", (self.collection, identity)
                ).fetchone()
                if old is not None:
                    if old[0] != payload:
                        raise ValueError("Ghost event identity conflict")
                    continue
                database.execute(
                    "INSERT INTO memory_events(collection,event_id,payload) VALUES(?,?,?)",
                    (self.collection, identity, payload),
                )

    def _check_cursor(self) -> None:
        database = self._db()
        tail_row: sqlite3.Row | None = database.execute(
            "SELECT coalesce(max(sequence),0) FROM memory_events WHERE collection=?", (self.collection,)
        ).fetchone()
        tail: object = tail_row[0] if tail_row is not None else None
        cursor: sqlite3.Row | None = database.execute(
            "SELECT applied_sequence FROM memory_consumers WHERE collection=?", (self.collection,)
        ).fetchone()
        position: object = cursor[0] if cursor is not None else 0
        if type(tail) is not int or type(position) is not int or tail != position:
            raise ValueError(f"Ghost {self.collection} consumer cursor does not match its events")

    def mark_applied(self) -> None:
        """Acknowledge control events or compaction that leave current records intact."""
        self._db().execute(
            "INSERT INTO memory_consumers(collection,applied_sequence) SELECT ?,coalesce(max(sequence),0) "
            "FROM memory_events WHERE collection=? ON CONFLICT(collection) DO UPDATE SET applied_sequence=excluded.applied_sequence",
            (self.collection, self.collection),
        )

    def replace_rows(self, rows: Iterable[Mapping[str, object]]) -> None:
        with self.transaction():
            database = self._db()
            database.execute("DELETE FROM memory_records WHERE collection=?", (self.collection,))
            for row in rows:
                database.execute(
                    "INSERT INTO memory_records(collection,id,scope,scope_ref,status,payload) VALUES(?,?,?,?,?,?)",
                    (
                        self.collection,
                        row["id"],
                        str(row.get("scope") or ""),
                        str(row.get("scope_ref") or ""),
                        str(row.get("status") or ""),
                        _encode(row),
                    ),
                )
            self.mark_applied()

    def retire_candidates(self, identities: Iterable[str]) -> None:
        """Fence explicitly deleted source identities in the caller's transaction."""
        if self.collection != "inbox":
            raise ValueError("only the inbox owns candidate retirement")
        self._db().executemany(
            "INSERT OR IGNORE INTO ghost_retired_inputs(kind,identity) VALUES('candidate',?)",
            ((identity,) for identity in identities),
        )

    def setting(self, key: str, default: object) -> object:
        with self.transaction(write=False):
            row: sqlite3.Row | None = self._db().execute(
                "SELECT value FROM ghost_settings WHERE key=?", (key,),
            ).fetchone()
            if row is None:
                return default
            encoded: object = row[0]
            if not isinstance(encoded, str):
                raise ValueError("Ghost setting must be encoded text")
            value: object = json.loads(encoded)
            return value

    def set_setting(self, key: str, value: object) -> None:
        with self.transaction():
            self._db().execute(
                "INSERT INTO ghost_settings(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (key, _encode(value)),
            )

    def reset(self, *, settings: bool = False) -> None:
        with self.transaction(deleting=True):
            database = self._db()
            if self.collection == "inbox":
                database.execute(
                    "INSERT OR IGNORE INTO ghost_retired_inputs(kind,identity) "
                    "SELECT 'candidate',id FROM memory_records WHERE collection='inbox'",
                )
            database.execute("DELETE FROM memory_records WHERE collection=?", (self.collection,))
            database.execute("DELETE FROM memory_events WHERE collection=?", (self.collection,))
            database.execute("DELETE FROM memory_consumers WHERE collection=?", (self.collection,))
            if settings:
                database.execute("DELETE FROM ghost_settings")

    def event_stats(self) -> dict[str, int]:
        with self.transaction(write=False):
            row: sqlite3.Row | None = (
                self._db()
                .execute(
                    "SELECT count(*),coalesce(sum(length(cast(payload AS BLOB))),0) "
                    "FROM memory_events WHERE collection=?",
                    (self.collection,),
                )
                .fetchone()
            )
            events: object = row[0] if row is not None else None
            size: object = row[1] if row is not None else None
            if type(events) is not int or type(size) is not int:
                raise ValueError("invalid Ghost record event statistics")
            return {"events": events, "bytes": size}

    def payload(self) -> dict[str, object] | None:
        rows = self.rows()
        return _payload(rows[0]["payload"]) if rows else None
