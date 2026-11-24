"""Cold-start schemas are admitted explicitly, never repaired by opening."""

import sqlite3
from contextlib import closing
from pathlib import Path

import pytest

from codey.durable import SQLiteDurableStore
from codey.durable.errors import SchemaVersionError


def _user_version(database: sqlite3.Connection) -> object:
    row: tuple[object, ...] = database.execute("PRAGMA user_version").fetchone()
    return row[0]


def test_fresh_cold_start_remains_v1_and_reopens_without_migration(tmp_path: Path) -> None:
    path = tmp_path / "durable.sqlite3"
    SQLiteDurableStore(path)
    with closing(sqlite3.connect(path)) as database:
        assert _user_version(database) == 1
        installed: list[object] = database.execute("SELECT version FROM schema_migrations").fetchall()
        assert installed == [(1,)]
        identity: object = database.execute("SELECT value FROM schema_metadata WHERE key='database_id'").fetchone()
    SQLiteDurableStore(path)
    with closing(sqlite3.connect(path)) as database:
        assert _user_version(database) == 1
        reopened: list[object] = database.execute("SELECT version FROM schema_migrations").fetchall()
        assert reopened == installed
        reopened_identity: object = database.execute("SELECT value FROM schema_metadata WHERE key='database_id'").fetchone()
        assert reopened_identity == identity


def test_newer_sqlite_user_version_is_rejected_without_downgrading(tmp_path: Path) -> None:
    path = tmp_path / "durable.sqlite3"
    SQLiteDurableStore(path)
    with closing(sqlite3.connect(path)) as database, database:
        database.execute("PRAGMA user_version=99")
    with pytest.raises(SchemaVersionError, match="schema"):
        SQLiteDurableStore(path)
    with closing(sqlite3.connect(path)) as database:
        assert _user_version(database) == 99


@pytest.mark.parametrize("damage", [
    "DROP TABLE effects",
    "ALTER TABLE commits DROP COLUMN payload",
    "ALTER TABLE schema_migrations ADD COLUMN extra TEXT",
    "ALTER TABLE schema_migrations RENAME COLUMN version TO obsolete",
])
def test_schema_markers_do_not_hide_missing_or_old_table_definitions(tmp_path: Path, damage: str) -> None:
    path = tmp_path / "durable.sqlite3"
    SQLiteDurableStore(path)
    with closing(sqlite3.connect(path)) as database, database:
        database.execute(damage)
    with pytest.raises(SchemaVersionError, match="schema"):
        SQLiteDurableStore(path)
