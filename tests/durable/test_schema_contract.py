"""Cold-start schemas are admitted explicitly, never repaired by opening."""

import sqlite3
from pathlib import Path

import pytest

from codey.durable import SQLiteDurableStore
from codey.durable.errors import SchemaVersionError


def test_newer_sqlite_user_version_is_rejected_without_downgrading(tmp_path: Path) -> None:
    path = tmp_path / "durable.sqlite3"
    SQLiteDurableStore(path)
    with sqlite3.connect(path) as database:
        database.execute("PRAGMA user_version=99")
    with pytest.raises(SchemaVersionError, match="schema"):
        SQLiteDurableStore(path)
    with sqlite3.connect(path) as database:
        assert database.execute("PRAGMA user_version").fetchone()[0] == 99


@pytest.mark.parametrize("damage", ["DROP TABLE effects", "ALTER TABLE commits DROP COLUMN payload"])
def test_schema_markers_do_not_hide_missing_or_old_table_definitions(tmp_path: Path, damage: str) -> None:
    path = tmp_path / "durable.sqlite3"
    SQLiteDurableStore(path)
    with sqlite3.connect(path) as database:
        database.execute(damage)
    with pytest.raises(SchemaVersionError, match="schema"):
        SQLiteDurableStore(path)
