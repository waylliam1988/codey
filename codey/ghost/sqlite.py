"""Small, independent SQLite owner for Ghost facts.

Ghost intentionally has its own database.  The task database must never wait
for memory maintenance, and Ghost must not pretend that two databases share a
transaction.  Connections are short lived and are never shared across
threads; each public mutation owns one explicit SQLite transaction.
"""

from __future__ import annotations

import sqlite3
import time
from collections.abc import Generator, Mapping
from contextlib import contextmanager
from contextvars import ContextVar
from pathlib import Path

from codey.durable.input_receipts import read_input_receipt

GHOST_SCHEMA_VERSION = 7
_PROJECTION_INPUTS: ContextVar[Mapping[Path, int] | None] = ContextVar("ghost_projection_inputs", default=None)


def _row_factory(cursor: sqlite3.Cursor, values: tuple[object, ...]) -> sqlite3.Row:
    row: sqlite3.Row = sqlite3.Row(cursor, values)
    return row


def _has_row(database: sqlite3.Connection, statement: str, parameters: tuple[object, ...] = ()) -> bool:
    row: sqlite3.Row | None = database.execute(statement, parameters).fetchone()
    return row is not None


class GhostSchemaVersionError(RuntimeError):
    """The Ghost database is newer than this cold-start build."""


class GhostProjectionInvalidatedError(OSError):
    """A deletion invalidated inputs collected before the write transaction."""


class GhostSQLite:
    """Open a fresh Ghost database and provide isolated connections."""

    def __init__(self, state_home: str | Path) -> None:
        self.path = Path(state_home) / "ghost.sqlite3"
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        database = sqlite3.connect(self.path, timeout=5.0)
        try:
            database.row_factory = _row_factory
            database.execute("PRAGMA foreign_keys=ON")
            database.execute("PRAGMA busy_timeout=5000")
            database.execute("PRAGMA synchronous=FULL")
            database.create_function("codey_input_receipt_ok", 2, self._input_receipt_ok)
        except BaseException:
            database.close()
            raise
        return database

    def _input_receipt_ok(self, database_name: object, identity: object) -> int:
        if database_name is None:
            return 1  # Independent Ghost stores have no canonical input owner.
        if (not isinstance(database_name, str) or Path(database_name).name != database_name
                or not database_name or not isinstance(identity, str) or not identity):
            return 0
        try:
            return int(read_input_receipt(self.path.with_name(database_name), identity, "ghost") is True)
        except (OSError, sqlite3.Error, TypeError, ValueError):
            return 0

    def bind_input_authority(self, database_name: str) -> None:
        """Bind run projections to the app's existing canonical input store."""
        if not database_name or Path(database_name).name != database_name:
            raise ValueError("canonical input authority must be in the same runtime directory")
        with self.connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            previous: sqlite3.Row | None = connection.execute(
                "SELECT database_name FROM ghost_input_authority WHERE singleton=1",
            ).fetchone()
            previous_name: object = None
            if previous is not None:
                previous_name = previous[0]
            if previous is not None and previous_name != database_name:
                raise ValueError("Ghost input authority is already bound to another store")
            connection.execute(
                "INSERT OR IGNORE INTO ghost_input_authority(singleton,database_name) VALUES(1,?)", (database_name,),
            )

    def input_is_accepted(self, connection: sqlite3.Connection, run_id: str) -> bool:
        row: sqlite3.Row | None = connection.execute(
            "SELECT codey_input_receipt_ok((SELECT database_name FROM ghost_input_authority WHERE singleton=1),?)",
            (run_id,),
        ).fetchone()
        result: object = None
        if row is not None:
            result = row[0]
        return result == 1

    @contextmanager
    def projection_inputs(self, *, run_ids: tuple[str, ...] = ()) -> Generator[None]:
        identity = self.path.resolve()
        inherited = _PROJECTION_INPUTS.get() or {}
        if identity in inherited and not run_ids:
            yield
            return
        with self.connection() as connection:
            generation = inherited.get(identity, self._deletion_generation(connection))
            if any(not self.input_is_accepted(connection, run_id) or _has_row(connection,
                "SELECT 1 FROM ghost_retired_inputs WHERE kind='observation' AND identity=?", (run_id,),
            ) for run_id in run_ids):
                generation = -1
        token = _PROJECTION_INPUTS.set({**inherited, identity: generation})
        try:
            yield
        finally:
            _PROJECTION_INPUTS.reset(token)

    def check_projection_inputs(self, connection: sqlite3.Connection) -> None:
        expected = (_PROJECTION_INPUTS.get() or {}).get(self.path.resolve())
        current = self._deletion_generation(connection)
        if expected is not None and current != expected:
            raise GhostProjectionInvalidatedError("Ghost projection inputs were invalidated by deletion")

    @staticmethod
    def _deletion_generation(connection: sqlite3.Connection) -> int:
        try:
            row: sqlite3.Row | None = connection.execute(
                "SELECT generation FROM ghost_projection_epoch WHERE singleton=1",
            ).fetchone()
        except sqlite3.Error as exc:
            raise GhostProjectionInvalidatedError("Ghost deletion generation is unreadable") from exc
        value: object = None
        if row is not None:
            value = row[0]
        if type(value) is not int or value < 0:
            raise GhostProjectionInvalidatedError("Ghost deletion generation is missing or invalid")
        return value

    @staticmethod
    def advance_deletion(connection: sqlite3.Connection) -> None:
        changed: int = connection.execute(
            "UPDATE ghost_projection_epoch SET generation=generation+1 WHERE singleton=1",
        ).rowcount
        if changed != 1:
            raise GhostProjectionInvalidatedError("Ghost deletion generation is missing")

    @staticmethod
    def _enable_wal(database: sqlite3.Connection) -> None:
        # journal_mode does not use SQLite's busy handler when two fresh
        # connections race to change it. Retry only that initialization lock.
        deadline = time.monotonic() + 5.0
        while True:
            try:
                row: sqlite3.Row | None = database.execute("PRAGMA journal_mode=WAL").fetchone()
                mode: object = None
                if row is not None:
                    mode = row[0]
                if mode != "wal":
                    raise RuntimeError("Ghost requires SQLite WAL journaling")
                return
            except sqlite3.OperationalError as exc:
                code = exc.sqlite_errorcode & 0xFF
                if code not in {sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED} or time.monotonic() >= deadline:
                    raise
                time.sleep(0.01)

    @staticmethod
    def _schema_version(database: sqlite3.Connection) -> int:
        row: sqlite3.Row | None = database.execute("PRAGMA user_version").fetchone()
        value: object = None
        if row is not None:
            value = row[0]
        if type(value) is not int or value < 0:
            raise GhostSchemaVersionError("Ghost schema version is missing or invalid")
        return value

    def _initialize(self) -> None:
        with self.connection() as database:
            version = self._schema_version(database)
            if version > GHOST_SCHEMA_VERSION:
                raise GhostSchemaVersionError("ghost database uses a newer schema")
            if version >= 3 and not _has_row(database,
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='ghost_retired_inputs'",
            ):
                raise GhostSchemaVersionError("Ghost deletion fences are missing from an existing schema")
            if version >= 4 and not _has_row(database,
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='ghost_projection_epoch'",
            ):
                raise GhostSchemaVersionError("Ghost projection generation is missing from an existing schema")
            if version >= 5 and not _has_row(database,
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='ghost_retired_sources'",
            ):
                raise GhostSchemaVersionError("Ghost source retirement is missing from an existing schema")
            if version >= 6 and not _has_row(database,
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='ghost_input_origins'",
            ):
                raise GhostSchemaVersionError("Ghost input origins are missing from an existing schema")
            if version >= 7 and not _has_row(database,
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='ghost_input_authority'",
            ):
                raise GhostSchemaVersionError("Ghost input authority is missing from an existing schema")
            self._enable_wal(database)
            database.executescript(
                """
                BEGIN IMMEDIATE;
                CREATE TABLE IF NOT EXISTS ghost_input_authority (
                    singleton INTEGER PRIMARY KEY CHECK(singleton=1),
                    database_name TEXT NOT NULL
                ) STRICT;
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
                CREATE TABLE IF NOT EXISTS ghost_deletion_manifests (
                    operation_id TEXT PRIMARY KEY,
                    status TEXT NOT NULL,
                    manifest TEXT NOT NULL CHECK(json_valid(manifest)),
                    updated_at TEXT NOT NULL
                ) STRICT;
                CREATE TABLE IF NOT EXISTS ghost_deletion_manifest_events (
                    sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                    operation_id TEXT NOT NULL REFERENCES ghost_deletion_manifests(operation_id) ON DELETE CASCADE,
                    event_id TEXT NOT NULL,
                    payload TEXT NOT NULL CHECK(json_valid(payload)),
                    UNIQUE(operation_id, event_id)
                ) STRICT;
                CREATE INDEX IF NOT EXISTS ghost_deletion_manifest_event_order
                    ON ghost_deletion_manifest_events(operation_id, sequence);
                CREATE TABLE IF NOT EXISTS ghost_retired_inputs (
                    kind TEXT NOT NULL CHECK(kind IN ('candidate','observation')),
                    identity TEXT NOT NULL,
                    PRIMARY KEY(kind,identity)
                ) STRICT;
                CREATE TABLE IF NOT EXISTS ghost_projection_epoch (
                    singleton INTEGER PRIMARY KEY CHECK(singleton=1),
                    generation INTEGER NOT NULL CHECK(generation>=0)
                ) STRICT;
                CREATE TABLE IF NOT EXISTS ghost_input_origins (
                    run_id TEXT PRIMARY KEY,
                    session_id TEXT NOT NULL,
                    project TEXT NOT NULL
                ) STRICT;
                CREATE INDEX IF NOT EXISTS ghost_input_origin_session ON ghost_input_origins(session_id);
                CREATE INDEX IF NOT EXISTS ghost_input_origin_project ON ghost_input_origins(project);
                CREATE TABLE IF NOT EXISTS ghost_retired_sources (
                    collection TEXT NOT NULL CHECK(collection IN ('continuity','work_queue')),
                    source TEXT NOT NULL CHECK(source<>'hebbian'),
                    source_ref TEXT NOT NULL,
                    scope TEXT NOT NULL,
                    scope_ref TEXT NOT NULL,
                    PRIMARY KEY(collection,source,source_ref,scope,scope_ref)
                ) STRICT;
                CREATE TRIGGER IF NOT EXISTS ghost_reject_retired_source_insert
                BEFORE INSERT ON memory_records
                WHEN EXISTS(
                    SELECT 1 FROM ghost_retired_sources AS retired
                    WHERE retired.collection=NEW.collection AND retired.scope=NEW.scope
                      AND retired.scope_ref=NEW.scope_ref
                      AND retired.source=json_extract(NEW.payload,'$.source')
                      AND retired.source_ref=json_extract(NEW.payload,'$.source_ref')
                )
                BEGIN SELECT RAISE(ABORT,'deleted Ghost source'); END;
                CREATE TRIGGER IF NOT EXISTS ghost_reject_retired_source_update
                BEFORE UPDATE ON memory_records
                WHEN EXISTS(
                    SELECT 1 FROM ghost_retired_sources AS retired
                    WHERE retired.collection=NEW.collection AND retired.scope=NEW.scope
                      AND retired.scope_ref=NEW.scope_ref
                      AND retired.source=json_extract(NEW.payload,'$.source')
                      AND retired.source_ref=json_extract(NEW.payload,'$.source_ref')
                )
                BEGIN SELECT RAISE(ABORT,'deleted Ghost source'); END;
                CREATE TRIGGER IF NOT EXISTS ghost_reject_retired_candidate
                BEFORE INSERT ON memory_records WHEN NEW.collection='inbox'
                  AND EXISTS(SELECT 1 FROM ghost_retired_inputs
                             WHERE kind='candidate' AND identity=NEW.id)
                BEGIN SELECT RAISE(ABORT,'deleted Ghost candidate'); END;
                CREATE TRIGGER IF NOT EXISTS ghost_reject_retired_graph_input
                BEFORE INSERT ON graph_nodes
                WHEN EXISTS(
                    SELECT 1 FROM json_each(NEW.payload,'$.candidate_ids') AS candidate
                    JOIN ghost_retired_inputs AS retired ON retired.identity=candidate.value
                    WHERE retired.kind='candidate'
                )
                BEGIN SELECT RAISE(ABORT,'deleted Ghost graph input'); END;
                CREATE TRIGGER IF NOT EXISTS ghost_reject_retired_observation
                BEFORE INSERT ON observation_events
                WHEN EXISTS(SELECT 1 FROM ghost_retired_inputs
                            WHERE kind='observation' AND identity=NEW.run_id)
                BEGIN SELECT RAISE(ABORT,'deleted Ghost observation'); END;
                CREATE TRIGGER IF NOT EXISTS ghost_reject_retired_observation_update
                BEFORE UPDATE ON observation_events
                WHEN EXISTS(SELECT 1 FROM ghost_retired_inputs
                            WHERE kind='observation' AND identity=NEW.run_id)
                BEGIN SELECT RAISE(ABORT,'deleted Ghost observation'); END;
                CREATE TRIGGER IF NOT EXISTS ghost_reject_retired_run_source_insert
                BEFORE INSERT ON memory_records
                WHEN NEW.collection IN ('continuity','work_queue')
                  AND (json_extract(NEW.payload,'$.source') IN ('task_done','run_ledger','work_checkpoint','review')
                       OR (json_extract(NEW.payload,'$.source')='continuity'
                           AND coalesce(json_extract(NEW.payload,'$.metadata.continuity_source'),'')
                               NOT IN ('hebbian','research_note')))
                  AND EXISTS(SELECT 1 FROM ghost_retired_inputs
                             WHERE kind='observation' AND identity=json_extract(NEW.payload,'$.source_ref'))
                BEGIN SELECT RAISE(ABORT,'deleted Ghost run source'); END;
                CREATE TRIGGER IF NOT EXISTS ghost_reject_retired_run_source_update
                BEFORE UPDATE ON memory_records
                WHEN NEW.collection IN ('continuity','work_queue')
                  AND (json_extract(NEW.payload,'$.source') IN ('task_done','run_ledger','work_checkpoint','review')
                       OR (json_extract(NEW.payload,'$.source')='continuity'
                           AND coalesce(json_extract(NEW.payload,'$.metadata.continuity_source'),'')
                               NOT IN ('hebbian','research_note')))
                  AND EXISTS(SELECT 1 FROM ghost_retired_inputs
                             WHERE kind='observation' AND identity=json_extract(NEW.payload,'$.source_ref'))
                BEGIN SELECT RAISE(ABORT,'deleted Ghost run source'); END;
                CREATE TRIGGER IF NOT EXISTS ghost_reject_unaccepted_observation_insert
                BEFORE INSERT ON observation_events
                WHEN NOT codey_input_receipt_ok(
                    (SELECT database_name FROM ghost_input_authority WHERE singleton=1),NEW.run_id)
                BEGIN SELECT RAISE(ABORT,'unaccepted Ghost input'); END;
                CREATE TRIGGER IF NOT EXISTS ghost_reject_unaccepted_observation_update
                BEFORE UPDATE ON observation_events
                WHEN NOT codey_input_receipt_ok(
                    (SELECT database_name FROM ghost_input_authority WHERE singleton=1),NEW.run_id)
                BEGIN SELECT RAISE(ABORT,'unaccepted Ghost input'); END;
                CREATE TRIGGER IF NOT EXISTS ghost_reject_unaccepted_run_source_insert
                BEFORE INSERT ON memory_records
                WHEN NEW.collection IN ('continuity','work_queue')
                  AND (json_extract(NEW.payload,'$.source') IN ('task_done','run_ledger','work_checkpoint','review')
                       OR (json_extract(NEW.payload,'$.source')='continuity'
                           AND coalesce(json_extract(NEW.payload,'$.metadata.continuity_source'),'')
                               NOT IN ('hebbian','research_note')))
                  AND NOT codey_input_receipt_ok(
                      (SELECT database_name FROM ghost_input_authority WHERE singleton=1),
                      json_extract(NEW.payload,'$.source_ref'))
                BEGIN SELECT RAISE(ABORT,'unaccepted Ghost run source'); END;
                CREATE TRIGGER IF NOT EXISTS ghost_reject_unaccepted_run_source_update
                BEFORE UPDATE ON memory_records
                WHEN NEW.collection IN ('continuity','work_queue')
                  AND (json_extract(NEW.payload,'$.source') IN ('task_done','run_ledger','work_checkpoint','review')
                       OR (json_extract(NEW.payload,'$.source')='continuity'
                           AND coalesce(json_extract(NEW.payload,'$.metadata.continuity_source'),'')
                               NOT IN ('hebbian','research_note')))
                  AND NOT codey_input_receipt_ok(
                      (SELECT database_name FROM ghost_input_authority WHERE singleton=1),
                      json_extract(NEW.payload,'$.source_ref'))
                BEGIN SELECT RAISE(ABORT,'unaccepted Ghost run source'); END;
                """
            )
            version = self._schema_version(database)
            if version > GHOST_SCHEMA_VERSION:
                raise GhostSchemaVersionError("ghost database uses a newer schema")
            if version < GHOST_SCHEMA_VERSION:
                if version < 4:
                    database.execute("INSERT INTO ghost_projection_epoch(singleton,generation) VALUES(1,0)")
                database.execute(f"PRAGMA user_version={GHOST_SCHEMA_VERSION}")
            # Recovery may be the first successful Ghost write after intake.
            # Bind even when the failed intake could not create Ghost metadata.
            if self.path.with_name("durable.sqlite3").exists():
                database.execute(
                    "INSERT OR IGNORE INTO ghost_input_authority(singleton,database_name) VALUES(1,'durable.sqlite3')",
                )

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
