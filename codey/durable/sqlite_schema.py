"""Cold-start schema owner for the SQLite durable backend."""

from __future__ import annotations

import sqlite3
import uuid
from collections.abc import Callable
from contextlib import closing
from datetime import UTC, datetime

from codey.durable.errors import SchemaVersionError
from codey.durable.schema import MIGRATION_MARKER_SQL, MIGRATIONS, SCHEMA_VERSION, validate_schema


def initialize_database(connect: Callable[[], sqlite3.Connection]) -> str:
    """Create or validate the exact cold-start schema and return its identity."""
    with closing(connect()) as database:
        database.execute("PRAGMA journal_mode=WAL")
        database.execute("BEGIN IMMEDIATE")
        try:
            # Read the version after taking the writer lock.  A concurrent
            # cold start may have created the migration marker but not yet
            # committed PRAGMA user_version; reading it before BEGIN IMMEDIATE
            # would cache that transient 0 and falsely reject a valid schema.
            version_row: sqlite3.Row | None = database.execute("PRAGMA user_version").fetchone()
            user_version: object = None
            if version_row is not None:
                user_version = version_row[0]
            if not isinstance(user_version, int):
                raise SchemaVersionError("durable database has an invalid schema version")
            if user_version > SCHEMA_VERSION:
                raise SchemaVersionError("durable database uses a newer schema")
            table_rows: list[sqlite3.Row] = database.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
            ).fetchall()
            tables: set[str] = set()
            for row in table_rows:
                name: object = row["name"]
                tables.add(str(name))
            if "schema_migrations" not in tables:
                if tables:
                    raise SchemaVersionError(
                        "durable database has tables but no schema migration marker"
                    )
                database.execute(MIGRATION_MARKER_SQL)
                fresh = True
                applied: tuple[int, ...] = ()
            else:
                validate_schema(database)
                applied = _applied_versions(database)
                if not applied:
                    raise SchemaVersionError(
                        "durable database has an empty schema migration marker"
                    )
                fresh = False
            if not fresh:
                if applied != (SCHEMA_VERSION,) or user_version != SCHEMA_VERSION:
                    raise SchemaVersionError(
                        "durable database is not the current cold-start schema"
                    )
                try:
                    marker = _metadata_value(database, "durable_schema")
                except sqlite3.OperationalError as exc:
                    raise SchemaVersionError(
                        "durable database is missing the current schema marker"
                    ) from exc
                if marker != str(SCHEMA_VERSION):
                    raise SchemaVersionError(
                        "durable database is missing the current schema marker"
                    )
            else:
                for version, statements in MIGRATIONS:
                    if version != SCHEMA_VERSION:
                        raise SchemaVersionError("durable migration table is incomplete")
                    for statement in statements:
                        database.execute(statement)
                    database.execute(
                        "INSERT INTO schema_migrations(version, applied_at) VALUES(?,?)",
                        (version, datetime.now(UTC).isoformat()),
                    )
                database.execute(
                    "INSERT INTO schema_metadata(key, value) VALUES('durable_schema', ?)",
                    (str(SCHEMA_VERSION),),
                )
                database.execute(
                    "INSERT INTO schema_metadata(key, value) VALUES('database_id', ?)",
                    (f"database-{uuid.uuid4().hex}",),
                )
                database.execute(
                    "INSERT INTO schema_metadata(key, value) VALUES('history_cursor_key', ?)",
                    (uuid.uuid4().hex + uuid.uuid4().hex,),
                )
                database.execute("INSERT INTO schema_metadata(key, value) VALUES('display_node_sequence', '0')")
            identity = _metadata_value(database, "database_id")
            if not isinstance(identity, str) or not identity.startswith("database-"):
                raise SchemaVersionError("durable database identity is missing or invalid")
            database.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
            database.execute("COMMIT")
            return identity
        except BaseException:
            if database.in_transaction:
                database.execute("ROLLBACK")
            raise


def _applied_versions(database: sqlite3.Connection) -> tuple[int, ...]:
    rows: list[sqlite3.Row] = database.execute("SELECT version FROM schema_migrations ORDER BY version").fetchall()
    versions: list[int] = []
    for row in rows:
        version: object = row["version"]
        if not isinstance(version, int):
            raise SchemaVersionError("durable database has an invalid schema migration marker")
        versions.append(version)
    return tuple(versions)


def _metadata_value(database: sqlite3.Connection, key: str) -> object:
    row: sqlite3.Row | None = database.execute("SELECT value FROM schema_metadata WHERE key=?", (key,)).fetchone()
    if row is None:
        return None
    value: object = row["value"]
    return value


__all__ = ["initialize_database"]
