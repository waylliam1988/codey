"""One memory journal and disposable projections in one SQLite transaction."""

from __future__ import annotations

import hashlib
import sqlite3
from collections.abc import Callable, Generator
from contextlib import contextmanager

from codey.memory.codec import canonical, event_from_text, event_payload
from codey.memory.contracts import MemoryEvent, MemoryHost, MemoryRebuildResult
from codey.types.json_value import JsonObject, parse_json_object

ProjectEvent = Callable[[sqlite3.Connection, MemoryEvent], None]


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
        "SELECT payload FROM projections WHERE collection = ? ORDER BY rowid", (collection,),
    )))


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
            raw: list[object] = connection.execute(
                "SELECT sequence, event_id, event_json, previous, digest FROM events ORDER BY sequence",
            ).fetchall()
            events: list[MemoryEvent] = []
            previous_digest = ""
            for index, row in enumerate(raw, 1):
                self.host.check_cancelled()
                if not isinstance(row, tuple):
                    raise ValueError("invalid memory journal row")
                cells: tuple[object, ...] = row
                if (len(cells) != 5 or cells[0] != index or not isinstance(cells[2], str)
                        or cells[3] != previous_digest):
                    raise ValueError("memory journal sequence is corrupt")
                current = hashlib.sha256((previous_digest + cells[2]).encode()).hexdigest()
                event = event_from_text(cells[2])
                if cells[1] != event.event_id or cells[4] != current or canonical(event_payload(event)) != cells[2]:
                    raise ValueError("memory journal content is corrupt")
                events.append(event)
                previous_digest = current
            head = canonical({"sequence": len(events), "digest": previous_digest})
            if texts(connection.execute("SELECT payload FROM journal_head WHERE id = 1")) != (head,):
                raise ValueError("memory journal head is corrupt")
            connection.execute("DELETE FROM projections")
            for event in events:
                self.host.check_cancelled()
                project(connection, event)
            state = [parse_json_object(text) for text in texts(connection.execute(
                "SELECT payload FROM projections ORDER BY collection, identity",
            ))]
            digest = hashlib.sha256(canonical({"records": state}).encode()).hexdigest()
            return MemoryRebuildResult(len(events), digest)
