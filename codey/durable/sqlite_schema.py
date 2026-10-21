"""Cold-start schema owner for the SQLite durable backend."""

from __future__ import annotations

import sqlite3
import uuid
from collections.abc import Callable
from contextlib import closing
from datetime import UTC, datetime

from codey.durable.errors import SchemaVersionError
from codey.durable.schema import MIGRATIONS, SCHEMA_VERSION, validate_schema


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
            user_version = int(database.execute("PRAGMA user_version").fetchone()[0])
            if user_version > SCHEMA_VERSION:
                raise SchemaVersionError("durable database uses a newer schema")
            tables = {
                str(row["name"])
                for row in database.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
                ).fetchall()
            }
            if "schema_migrations" not in tables:
                if tables:
                    raise SchemaVersionError(
                        "durable database has tables but no schema migration marker"
                    )
                database.execute(
                    "CREATE TABLE schema_migrations "
                    "(version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL) STRICT"
                )
                fresh = True
                applied: tuple[int, ...] = ()
            else:
                rows = database.execute(
                    "SELECT version FROM schema_migrations ORDER BY version"
                ).fetchall()
                applied = tuple(int(row["version"]) for row in rows)
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
                    marker = database.execute(
                        "SELECT value FROM schema_metadata WHERE key='durable_schema'"
                    ).fetchone()
                except sqlite3.OperationalError as exc:
                    raise SchemaVersionError(
                        "durable database is missing the current schema marker"
                    ) from exc
                if marker is None or str(marker["value"]) != str(SCHEMA_VERSION):
                    raise SchemaVersionError(
                        "durable database is missing the current schema marker"
                    )
                validate_schema(database)
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
            identity = database.execute(
                "SELECT value FROM schema_metadata WHERE key='database_id'"
            ).fetchone()
            if identity is None or not str(identity["value"]).startswith("database-"):
                raise SchemaVersionError("durable database identity is missing or invalid")
            database_id = str(identity["value"])
            database.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
            database.execute("COMMIT")
            return database_id
        except BaseException:
            if database.in_transaction:
                database.execute("ROLLBACK")
            raise


__all__ = ["initialize_database"]
