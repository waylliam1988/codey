"""Small, independent SQLite owner for Ghost facts.

Ghost intentionally has its own database.  The task database must never wait
for memory maintenance, and Ghost must not pretend that two databases share a
transaction.  Connections are short lived and are never shared across
threads; each public mutation owns one explicit SQLite transaction.
"""

from __future__ import annotations

import sqlite3
import time
from collections.abc import Generator
from contextlib import contextmanager
from pathlib import Path

GHOST_SCHEMA_VERSION = 1


class GhostSchemaVersionError(RuntimeError):
    """The Ghost database is newer than this cold-start build."""


class GhostSQLite:
    """Open a fresh Ghost database and provide isolated connections."""

    def __init__(self, state_home: str | Path) -> None:
        self.path = Path(state_home) / "ghost.sqlite3"
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        database = sqlite3.connect(self.path, timeout=5.0)
        try:
            database.row_factory = sqlite3.Row
            database.execute("PRAGMA foreign_keys=ON")
            database.execute("PRAGMA busy_timeout=5000")
            database.execute("PRAGMA synchronous=FULL")
        except BaseException:
            database.close()
            raise
        return database

    @staticmethod
    def _enable_wal(database: sqlite3.Connection) -> None:
        # journal_mode does not use SQLite's busy handler when two fresh
        # connections race to change it. Retry only that initialization lock.
        deadline = time.monotonic() + 5.0
        while True:
            try:
                mode = database.execute("PRAGMA journal_mode=WAL").fetchone()[0]
                if mode != "wal":
                    raise RuntimeError("Ghost requires SQLite WAL journaling")
                return
            except sqlite3.OperationalError as exc:
                code = getattr(exc, "sqlite_errorcode", 0) & 0xFF
                if code not in {sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED} or time.monotonic() >= deadline:
                    raise
                time.sleep(0.01)

    def _initialize(self) -> None:
        with self.connection() as database:
            version = int(database.execute("PRAGMA user_version").fetchone()[0])
            if version > GHOST_SCHEMA_VERSION:
                raise GhostSchemaVersionError("ghost database uses a newer schema")
            self._enable_wal(database)
            database.executescript(
                """
                BEGIN IMMEDIATE;
                CREATE TABLE IF NOT EXISTS observation_events (
                    sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                    run_id TEXT NOT NULL UNIQUE,
                    session_id TEXT NOT NULL,
                    project TEXT NOT NULL,
                    mode TEXT NOT NULL,
                    user_text TEXT NOT NULL,
                    assistant_text TEXT NOT NULL,
                    stop_reason TEXT NOT NULL,
                    committed INTEGER NOT NULL CHECK(committed IN (0, 1)),
                    provider_id TEXT NOT NULL,
                    ts TEXT NOT NULL
                ) STRICT;
                CREATE INDEX IF NOT EXISTS observation_by_session
                    ON observation_events(session_id, sequence DESC);
                CREATE INDEX IF NOT EXISTS observation_by_project
                    ON observation_events(project, sequence DESC);
                CREATE INDEX IF NOT EXISTS observation_by_committed
                    ON observation_events(committed, sequence DESC);
                CREATE TABLE IF NOT EXISTS memory_embeddings (
                    run_id TEXT PRIMARY KEY REFERENCES observation_events(run_id) ON DELETE CASCADE,
                    model_id TEXT NOT NULL,
                    dimensions INTEGER NOT NULL CHECK(dimensions BETWEEN 1 AND 8192),
                    content_digest TEXT NOT NULL,
                    vector BLOB NOT NULL CHECK(length(vector)=dimensions*4)
                ) STRICT;
                CREATE TABLE IF NOT EXISTS graph_events (
                    sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                    graph TEXT NOT NULL CHECK(graph IN ('hebbian','affinity')),
                    event_id TEXT NOT NULL,
                    payload TEXT NOT NULL CHECK(json_valid(payload)),
                    UNIQUE(graph,event_id)
                ) STRICT;
                CREATE INDEX IF NOT EXISTS graph_event_order ON graph_events(graph,sequence);
                CREATE TABLE IF NOT EXISTS graph_consumers (
                    graph TEXT PRIMARY KEY CHECK(graph IN ('hebbian','affinity')),
                    applied_sequence INTEGER NOT NULL CHECK(applied_sequence>=0)
                ) STRICT;
                CREATE TABLE IF NOT EXISTS graph_nodes (
                    graph TEXT NOT NULL,
                    id TEXT NOT NULL,
                    kind TEXT NOT NULL,
                    scope TEXT NOT NULL,
                    scope_ref TEXT NOT NULL,
                    status TEXT NOT NULL,
                    weight REAL NOT NULL CHECK(weight BETWEEN 0 AND 1),
                    payload TEXT NOT NULL CHECK(json_valid(payload)),
                    PRIMARY KEY(graph,id)
                ) STRICT;
                CREATE INDEX IF NOT EXISTS graph_node_scope ON graph_nodes(graph,scope,scope_ref,status,weight DESC);
                CREATE TABLE IF NOT EXISTS graph_edges (
                    graph TEXT NOT NULL,
                    source TEXT NOT NULL,
                    target TEXT NOT NULL,
                    relation TEXT NOT NULL,
                    weight REAL NOT NULL CHECK(weight BETWEEN 0 AND 1),
                    payload TEXT NOT NULL CHECK(json_valid(payload)),
                    PRIMARY KEY(graph,source,target,relation),
                    FOREIGN KEY(graph,source) REFERENCES graph_nodes(graph,id) ON DELETE CASCADE,
                    FOREIGN KEY(graph,target) REFERENCES graph_nodes(graph,id) ON DELETE CASCADE
                ) STRICT;
                CREATE TABLE IF NOT EXISTS memory_events (
                    sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                    collection TEXT NOT NULL CHECK(collection IN ('inbox','continuity','work_queue','sleep')),
                    event_id TEXT NOT NULL,
                    payload TEXT NOT NULL CHECK(json_valid(payload)),
                    UNIQUE(collection,event_id)
                ) STRICT;
                CREATE INDEX IF NOT EXISTS memory_event_order ON memory_events(collection,sequence);
                CREATE TABLE IF NOT EXISTS memory_consumers (
                    collection TEXT PRIMARY KEY,
                    applied_sequence INTEGER NOT NULL CHECK(applied_sequence>=0)
                ) STRICT;
                CREATE TABLE IF NOT EXISTS memory_records (
                    collection TEXT NOT NULL CHECK(collection IN ('inbox','continuity','work_queue','sleep')),
                    id TEXT NOT NULL,
                    scope TEXT NOT NULL,
                    scope_ref TEXT NOT NULL,
                    status TEXT NOT NULL,
                    payload TEXT NOT NULL CHECK(json_valid(payload)),
                    PRIMARY KEY(collection,id)
                ) STRICT;
                CREATE INDEX IF NOT EXISTS memory_record_scope ON memory_records(collection,scope,scope_ref,status);
                CREATE TABLE IF NOT EXISTS ghost_settings (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL CHECK(json_valid(value))
                ) STRICT;
                """
            )
            version = int(database.execute("PRAGMA user_version").fetchone()[0])
            if version > GHOST_SCHEMA_VERSION:
                raise GhostSchemaVersionError("ghost database uses a newer schema")
            if version < GHOST_SCHEMA_VERSION:
                database.execute(f"PRAGMA user_version={GHOST_SCHEMA_VERSION}")

    @contextmanager
    def connection(self) -> Generator[sqlite3.Connection, None, None]:
        """Yield one connection and commit or roll back its transaction."""
        database = self._connect()
        try:
            yield database
            database.commit()
        except BaseException:
            database.rollback()
            raise
        finally:
            database.close()


__all__ = ["GHOST_SCHEMA_VERSION", "GhostSQLite", "GhostSchemaVersionError"]
