"""Only the known backward-compatible capture extension admits activation."""
from __future__ import annotations

import sqlite3
from contextlib import closing
from pathlib import Path

import pytest

from codey.launcher.runner import schema
from codey.storage.execution_output_store import SCHEMA, ExecutionOutputStore


def legacy_database(home: Path) -> None:
    home.mkdir(exist_ok=True)
    with closing(sqlite3.connect(home / "execution_outputs.sqlite3")) as db:
        db.executescript(SCHEMA)
        db.execute("INSERT INTO execution_outputs VALUES ('old','s','r','settled',1,'old body','',8,0,0,1024)")
        db.commit()


def test_metadata_migration_keeps_activation_and_legacy_writer_contract(tmp_path: Path) -> None:
    legacy_database(tmp_path)
    before = schema(tmp_path)
    store = ExecutionOutputStore(tmp_path)
    assert schema(tmp_path) == before
    assert store.snapshot("old")["stdout"]["text"] == "old body"
    # The captured previous runtime names columns explicitly; adding defaulted
    # metadata must keep its INSERT, UPDATE and SELECT contracts operational.
    with closing(sqlite3.connect(store.path)) as db:
        db.execute("INSERT INTO execution_outputs(execution_id,session_id,run_id,state,revision,stdout,stderr,stdout_bytes,stderr_bytes,exit_code,max_bytes) VALUES ('legacy-new','s','r','running',0,'','',0,0,NULL,1024)")
        db.execute("UPDATE execution_outputs SET stdout='legacy output',stdout_bytes=13 WHERE execution_id='legacy-new'")
        db.commit()
        assert db.execute("SELECT stdout,stderr,stdout_bytes,stderr_bytes,max_bytes,revision,state,exit_code FROM execution_outputs WHERE execution_id='legacy-new'").fetchone()[0] == "legacy output"
    assert store.snapshot("legacy-new")["stdout"]["text"] == "legacy output"
    store.close()


@pytest.mark.parametrize("alteration", [
    "ALTER TABLE execution_outputs ADD COLUMN extra TEXT",
    "ALTER TABLE execution_outputs ADD COLUMN stdout_meta TEXT NOT NULL DEFAULT 'wrong'",
    "ALTER TABLE execution_outputs ADD COLUMN stdout_meta INTEGER NOT NULL DEFAULT '{}'",
    "CREATE INDEX changed ON execution_outputs(state)",
    "PRAGMA user_version=2",
])
def test_unapproved_schema_changes_still_block(tmp_path: Path, alteration: str) -> None:
    legacy_database(tmp_path)
    before = schema(tmp_path)
    with closing(sqlite3.connect(tmp_path / "execution_outputs.sqlite3")) as db:
        db.execute(alteration)
    assert schema(tmp_path) != before
