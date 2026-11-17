"""Ghost candidate, continuity, queue and sleep records with their event cursors."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from collections.abc import Generator, Iterable, Mapping
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Literal, Protocol, TypeVar

from codey.ghost.payload_types import decoded_object
from codey.ghost.sqlite import GhostSQLite


class _SourceProjection(Protocol):
    @property
    def source(self) -> str: ...
    @property
    def source_ref(self) -> str: ...
    @property
    def scope(self) -> str: ...
    @property
    def scope_ref(self) -> str: ...
    @property
    def metadata(self) -> Mapping[str, object]: ...


_SourceItem = TypeVar("_SourceItem", bound=_SourceProjection)


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


def _decoded_row(row: sqlite3.Row) -> dict[str, object]:
    encoded: object = row[0]
    return decoded_object(encoded)


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
            if not connection.in_transaction:
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
            return [_decoded_row(row) for row in rows]

    def rows(self) -> list[dict[str, object]]:
        with self.transaction(write=False):
            self._check_cursor()
            rows: Iterable[sqlite3.Row] = self._db().execute(
                "SELECT payload FROM memory_records WHERE collection=? ORDER BY id", (self.collection,),
            )
            return [_decoded_row(row) for row in rows]

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
                    previous: object = old[0]
                    if previous != payload:
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
        tail: object = None
        if tail_row is not None:
            tail = tail_row[0]
        cursor: sqlite3.Row | None = database.execute(
            "SELECT applied_sequence FROM memory_consumers WHERE collection=?", (self.collection,)
        ).fetchone()
        position: object = 0
        if cursor is not None:
            position = cursor[0]
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

    def retire_sources(self, items: Iterable[_SourceProjection]) -> None:
        """Retire explicitly removed sources without changing their runtime owner."""
        if self.collection not in {"continuity", "work_queue"}:
            raise ValueError("this collection does not own derived source retirement")
        self._db().executemany(
            "INSERT OR IGNORE INTO ghost_retired_sources(collection,source,source_ref,scope,scope_ref) "
            "VALUES(?,?,?,?,?)",
            (
                (self.collection, item.source, item.source_ref, item.scope, item.scope_ref)
                # A Hebbian node id names a reusable preference slot. Its
                # candidate identities already have their own retirement gate.
                for item in items if item.source and item.source != "hebbian" and item.source_ref
            ),
        )

    def live_sources(self, items: Iterable[_SourceItem]) -> tuple[_SourceItem, ...]:
        """Filter a freshly read source set inside the caller's store transaction."""
        live: list[_SourceItem] = []
        for item in items:
            # Use the primary key rather than scanning a permanent retirement
            # history whose size is independent of the bounded source set.
            row: sqlite3.Row | None = self._db().execute(
                "SELECT 1 FROM ghost_retired_sources "
                "WHERE collection=? AND source=? AND source_ref=? AND scope=? AND scope_ref=?",
                (self.collection, item.source, item.source_ref, item.scope, item.scope_ref),
            ).fetchone()
            is_run_source = item.source in {"task_done", "run_ledger", "work_checkpoint", "review"} or (
                item.source == "continuity" and item.metadata.get("continuity_source") not in ("hebbian", "research_note")
            )
            if row is None and is_run_source:
                if not self.database.input_is_accepted(self._db(), item.source_ref):
                    continue
                row = self._db().execute(
                    "SELECT 1 FROM ghost_retired_inputs WHERE kind='observation' AND identity=?", (item.source_ref,),
                ).fetchone()
            if row is None:
                live.append(item)
        return tuple(live)

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
            if self.collection in {"continuity", "work_queue"}:
                # Reset must still work when a projection cursor is damaged.
                # Extract only actual source identities; malformed records do
                # not provide an identity that can safely be invented.
                database.execute(
                    "INSERT OR IGNORE INTO ghost_retired_sources(collection,source,source_ref,scope,scope_ref) "
                    "SELECT collection,json_extract(payload,'$.source'),json_extract(payload,'$.source_ref'),scope,scope_ref "
                    "FROM memory_records WHERE collection=? "
                    "AND json_type(payload,'$.source')='text' AND length(json_extract(payload,'$.source'))>0 "
                    "AND json_extract(payload,'$.source')<>'hebbian' "
                    "AND json_type(payload,'$.source_ref')='text' AND length(json_extract(payload,'$.source_ref'))>0",
                    (self.collection,),
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
            events: object = None
            size: object = None
            if row is not None:
                events = row[0]
                size = row[1]
            if type(events) is not int or type(size) is not int:
                raise ValueError("invalid Ghost record event statistics")
            return {"events": events, "bytes": size}

    def payload(self) -> dict[str, object] | None:
        rows = self.rows()
        return _payload(rows[0]["payload"]) if rows else None
