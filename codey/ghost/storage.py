"""One memory journal and disposable projections in one SQLite transaction."""

from __future__ import annotations

import hashlib
import sqlite3
from collections.abc import Callable, Generator
from contextlib import contextmanager

from codey.memory.codec import canonical, event_from_text, event_payload, timestamp
from codey.memory.contracts import MemoryEvent, MemoryHost, MemoryRebuildResult
from codey.types.json_value import (
    JsonObject,
    JsonValue,
    decode_json_object,
    parse_json_object,
    require_json_integer,
    require_json_string,
)

ProjectEvent = Callable[[sqlite3.Connection, MemoryEvent], None]


def journal_rows(connection: sqlite3.Connection) -> Generator[tuple[object, ...], None, None]:
    cursor = connection.execute("SELECT sequence, event_id, event_json, previous, digest FROM events ORDER BY sequence")
    while True:
        batch: list[object] = cursor.fetchmany(256)
        if not batch:
            return
        for row in batch:
            if not isinstance(row, tuple):
                raise ValueError("invalid memory journal row")
            cells: tuple[object, ...] = row
            yield cells


def texts(cursor: sqlite3.Cursor) -> tuple[str, ...]:
    raw: list[object] = cursor.fetchall()
    result: list[str] = []
    for row in raw:
        if not isinstance(row, tuple):
            raise ValueError("invalid memory database row")
        cells: tuple[object, ...] = row
        if len(cells) != 1 or not isinstance(cells[0], str):
            raise ValueError("invalid memory database value")
        result.append(cells[0])
    return tuple(result)


def rows(connection: sqlite3.Connection, collection: str) -> tuple[JsonObject, ...]:
    return tuple(parse_json_object(text) for text in texts(connection.execute(
        "SELECT payload FROM projections WHERE collection = ? ORDER BY identity", (collection,),
    )))


def scoped_rows(connection: sqlite3.Connection, collection: str, scopes: tuple[str, ...]) -> tuple[JsonObject, ...]:
    if not scopes:
        return rows(connection, collection)
    placeholders = ",".join("?" for _ in scopes)
    return tuple(parse_json_object(value) for value in texts(connection.execute(
        "SELECT payload FROM projections WHERE collection = ? AND scope IN (" + placeholders + ") ORDER BY identity",
        (collection, *scopes))))


def load(connection: sqlite3.Connection, collection: str, identity: str) -> JsonObject | None:
    found = texts(connection.execute(
        "SELECT payload FROM projections WHERE collection = ? AND identity = ?", (collection, identity),
    ))
    return parse_json_object(found[0]) if found else None


def save(connection: sqlite3.Connection, collection: str, identity: str, scope: str, value: JsonObject) -> None:
    connection.execute(
        "INSERT INTO projections VALUES (?, ?, ?, ?) ON CONFLICT(collection, identity) "
        "DO UPDATE SET scope = excluded.scope, payload = excluded.payload",
        (collection, identity, scope, canonical(value)),
    )


def _retained_identities(value: JsonValue) -> dict[str, str]:
    if not isinstance(value, list):
        raise ValueError("invalid retained memory event identities")
    identities: dict[str, str] = {}
    for pair in value:
        if not isinstance(pair, list) or len(pair) != 2:
            raise ValueError("invalid retained memory event identity")
        identity = require_json_string(pair[0], "event identity")
        if identity in identities:
            raise ValueError("duplicate retained memory event identity")
        identities[identity] = require_json_string(pair[1], "event digest")
    return identities


class GhostDatabase:
    def __init__(self, host: MemoryHost) -> None:
        self.host = host
        self.path = host.state_dir / "memory.sqlite3"

    @contextmanager
    def connect(self, *, write: bool = False) -> Generator[sqlite3.Connection, None, None]:
        self.host.check_cancelled()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(self.path, timeout=0.05)
        try:
            connection.execute("PRAGMA secure_delete = ON")
            legacy = texts(connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table' AND name = 'events' "
                "AND sql NOT LIKE '%event_json%'",
            ))
            if legacy:
                raise ValueError("memory journal schema changed; explicitly reset the obsolete test state")
            connection.executescript("""
                CREATE TABLE IF NOT EXISTS events (
                    sequence INTEGER PRIMARY KEY, event_id TEXT UNIQUE NOT NULL,
                    event_json TEXT NOT NULL, previous TEXT NOT NULL, digest TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS journal_head (id INTEGER PRIMARY KEY CHECK (id = 1), payload TEXT NOT NULL);
                INSERT OR IGNORE INTO journal_head VALUES (1, '{"digest":"","sequence":0}');
                CREATE TABLE IF NOT EXISTS projections (
                    collection TEXT NOT NULL, identity TEXT NOT NULL, scope TEXT NOT NULL, payload TEXT NOT NULL,
                    PRIMARY KEY (collection, identity)
                );
                CREATE INDEX IF NOT EXISTS projection_scope ON projections(collection, scope);
                CREATE TABLE IF NOT EXISTS recovery_base (id INTEGER PRIMARY KEY CHECK (id = 1), payload TEXT NOT NULL);
            """)
            if write:
                connection.execute("BEGIN IMMEDIATE")
            yield connection
            connection.commit()
        finally:
            connection.close()

    def append(self, event: MemoryEvent, project: ProjectEvent) -> None:
        value = canonical(event_payload(event))
        with self.connect(write=True) as connection:
            base_rows = texts(connection.execute("SELECT payload FROM recovery_base WHERE id = 1"))
            if base_rows:
                base = self._base(base_rows[0])
                identities = _retained_identities(base.get("identities"))
                retired = identities.get(event.event_id)
                if retired is not None:
                    if retired != hashlib.sha256(value.encode()).hexdigest():
                        raise ValueError("memory event identity already names different content")
                    return
                retired_before = require_json_string(base.get("retired_before"), "retired_before")
                if event.kind not in {"task.completed", "task.failed", "memory.proposed", "memory.observation_checkpoint"} and timestamp(event.occurred_at) <= timestamp(retired_before):
                    raise ValueError("memory command is outside the retained retry window")
                floor = require_json_string(base.get("intake_before"), "intake_before")
                if floor and event.kind in {"task.completed", "task.failed", "memory.proposed"} and timestamp(event.occurred_at) <= timestamp(floor):
                    return
            previous = texts(connection.execute("SELECT event_json FROM events WHERE event_id = ?", (event.event_id,)))
            if previous:
                if previous[0] != value:
                    raise ValueError("memory event identity already names different content")
                return
            head = parse_json_object(texts(connection.execute("SELECT payload FROM journal_head WHERE id = 1"))[0])
            sequence, previous_digest = head.get("sequence"), head.get("digest")
            if type(sequence) is not int or not isinstance(previous_digest, str):
                raise ValueError("invalid memory journal head")
            project(connection, event)
            current = hashlib.sha256((previous_digest + value).encode()).hexdigest()
            connection.execute("INSERT INTO events VALUES (?, ?, ?, ?, ?)",
                               (sequence + 1, event.event_id, value, previous_digest, current))
            connection.execute("UPDATE journal_head SET payload = ? WHERE id = 1",
                               (canonical({"sequence": sequence + 1, "digest": current}),))

    def rebuild(self, project: ProjectEvent) -> MemoryRebuildResult:
        with self.connect(write=True) as connection:
            # Validate the complete journal before changing any live projection.
            base_rows = texts(connection.execute("SELECT payload FROM recovery_base WHERE id = 1"))
            base = self._base(base_rows[0]) if base_rows else None
            sequence = require_json_integer(base.get("sequence"), "sequence") if base else 0
            previous_digest = require_json_string(base.get("journal_digest"), "journal_digest") if base else ""
            count = 0
            for index, cells in enumerate(journal_rows(connection), sequence + 1):
                self.host.check_cancelled()
                if (len(cells) != 5 or cells[0] != index or not isinstance(cells[2], str)
                        or cells[3] != previous_digest):
                    raise ValueError("memory journal sequence is corrupt")
                current = hashlib.sha256((previous_digest + cells[2]).encode()).hexdigest()
                event = event_from_text(cells[2])
                if cells[1] != event.event_id or cells[4] != current or canonical(event_payload(event)) != cells[2]:
                    raise ValueError("memory journal content is corrupt")
                count += 1
                previous_digest = current
            head = canonical({"sequence": sequence + count, "digest": previous_digest})
            if texts(connection.execute("SELECT payload FROM journal_head WHERE id = 1")) != (head,):
                raise ValueError("memory journal head is corrupt")
            connection.execute("DELETE FROM projections")
            if base is not None:
                records = base.get("records")
                if not isinstance(records, list):
                    raise ValueError("invalid recovery base records")
                for value in records:
                    row = decode_json_object(value)
                    save(connection, require_json_string(row.get("collection"), "collection"),
                         require_json_string(row.get("identity"), "identity"),
                         require_json_string(row.get("scope"), "scope"), decode_json_object(row.get("payload")))
            for cells in journal_rows(connection):
                self.host.check_cancelled()
                event_text = cells[2]
                if not isinstance(event_text, str):
                    raise ValueError("invalid validated journal payload")
                project(connection, event_from_text(event_text))
            state = [parse_json_object(text) for text in texts(connection.execute(
                "SELECT payload FROM projections ORDER BY collection, identity",
            ))]
            digest = hashlib.sha256(canonical({"records": state}).encode()).hexdigest()
            return MemoryRebuildResult(sequence + count, digest)

    @staticmethod
    def _base(text: str) -> JsonObject:
        base = parse_json_object(text)
        expected = base.pop("checksum", None)
        if expected != hashlib.sha256(canonical(base).encode()).hexdigest():
            raise ValueError("memory recovery base is corrupt")
        if base.get("schema_version") != 2:
            raise ValueError("unsupported memory snapshot schema")
        if base.get("projector_version") != 3:
            raise ValueError("unsupported memory projector recovery version")
        return base

    def compact(self, *, as_of: str, retired_before: str, keep_events: int, project: ProjectEvent) -> MemoryRebuildResult:
        if keep_events < 0:
            raise ValueError("retained event count cannot be negative")
        # Verify before touching the recovery base. Retention has already been
        # committed as an ordinary deterministic event.
        self.rebuild(project)
        with self.connect(write=True) as connection:
            head = parse_json_object(texts(connection.execute("SELECT payload FROM journal_head WHERE id = 1"))[0])
            prior_rows = texts(connection.execute("SELECT payload FROM recovery_base WHERE id = 1"))
            prior = self._base(prior_rows[0]) if prior_rows else None
            identities = _retained_identities(prior.get("identities")) if prior else {}
            latest = texts(connection.execute("SELECT event_json FROM events ORDER BY sequence DESC LIMIT ?", (keep_events,)))
            for text in reversed(latest):
                identities[event_from_text(text).event_id] = hashlib.sha256(text.encode()).hexdigest()
            # Keep a bounded retry window. Older task inputs are additionally
            # excluded by the intake floor, including after Core backfill.
            latest_identities: list[tuple[str, str]] = list(identities.items())[-keep_events:] if keep_events else []
            retained: list[JsonValue] = [[identity, digest] for identity, digest in latest_identities]
            raw: list[object] = connection.execute(
                "SELECT collection, identity, scope, payload FROM projections ORDER BY collection, identity").fetchall()
            records: list[JsonValue] = []
            for value in raw:
                if not isinstance(value, tuple):
                    raise ValueError("invalid recovery projection")
                cells: tuple[object, ...] = value
                if len(cells) != 4 or not all(isinstance(cell, str) for cell in cells):
                    raise ValueError("invalid recovery projection fields")
                collection, identity, scope, payload = cells
                if not isinstance(payload, str):
                    raise ValueError("invalid recovery projection payload")
                records.append({"collection": str(collection), "identity": str(identity), "scope": str(scope), "payload": parse_json_object(payload)})
            payloads = [decode_json_object(value)["payload"] for value in records]
            state_digest = hashlib.sha256(canonical({"records": payloads}).encode()).hexdigest()
            verified = MemoryRebuildResult(require_json_integer(head.get("sequence"), "sequence"), state_digest)
            base: JsonObject = {"schema_version": 2, "projector_version": 3, "sequence": head.get("sequence"),
                "journal_digest": head.get("digest"), "state_digest": state_digest, "records": records,
                "identities": retained, "intake_before": as_of, "retired_before": retired_before}
            base["checksum"] = hashlib.sha256(canonical(base).encode()).hexdigest()
            connection.execute("INSERT INTO recovery_base VALUES (1, ?) ON CONFLICT(id) DO UPDATE SET payload=excluded.payload", (canonical(base),))
            connection.execute("DELETE FROM events")
        with self.connect() as connection:
            connection.execute("VACUUM")
        return verified
