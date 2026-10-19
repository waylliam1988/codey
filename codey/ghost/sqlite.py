"""Small, independent SQLite owner for Ghost facts.

Ghost intentionally has its own database.  The task database must never wait
for memory maintenance, and Ghost must not pretend that two databases share a
transaction.  Connections are short lived and are never shared across
threads; each public mutation owns one explicit SQLite transaction.
"""

from __future__ import annotations

import sqlite3
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
        database.row_factory = sqlite3.Row
        database.execute("PRAGMA foreign_keys=ON")
        database.execute("PRAGMA busy_timeout=5000")
        database.execute("PRAGMA journal_mode=WAL")
        database.execute("PRAGMA synchronous=FULL")
        return database

    def _initialize(self) -> None:
        with self.connection() as database:
            version = int(database.execute("PRAGMA user_version").fetchone()[0])
            if version > GHOST_SCHEMA_VERSION:
                raise GhostSchemaVersionError("ghost database uses a newer schema")
            database.executescript(
                """
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
                """
            )
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
