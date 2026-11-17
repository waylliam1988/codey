"""Schema-nine migration preserves history while fencing historical consumers."""

from __future__ import annotations

import sqlite3
from collections.abc import Iterable
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from pathlib import Path

import pytest

import codey.ghost.sqlite as sqlite_module
from codey.ghost.embeddings import pack_embedding
from codey.ghost.event_windows import event_window
from codey.ghost.observations import GhostObservationStore
from codey.ghost.sqlite import GHOST_SCHEMA_VERSION, GhostSchemaVersionError, GhostSQLite, _row_factory
from codey.ghost.vectors import observation_digest
from tests.test_ghost_event_windows import _advance_observations
from tests.test_ghost_legacy_observation_reader import _reader
from tests.test_ghost_scope_history_retention import _embedding_ids, _Embeddings

LEGACY_LAYOUTS = (("7244c1e3", 7), ("8cffa97c", 8))


def _rows(database: sqlite3.Connection, statement: str) -> tuple[tuple[object, ...], ...]:
    rows: Iterable[sqlite3.Row] = database.execute(statement)
    result: list[tuple[object, ...]] = []
    for row in rows:
        fields: list[object] = []
        for index in range(len(row)):
            field: object = row[index]
            fields.append(field)
        result.append(tuple(fields))
    return tuple(result)


def _open(home: Path) -> None:
    GhostSQLite(home)


@pytest.mark.parametrize(("revision", "schema"), LEGACY_LAYOUTS)
@pytest.mark.parametrize("concurrent", [False, True])
def test_real_upgrade_preserves_rows_sequences_foreign_keys_and_cached_vectors(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, revision: str, schema: int, concurrent: bool,
) -> None:
    for name in ("GHOST_EMBEDDING_URL", "GHOST_EMBEDDING_MODEL", "GHOST_EMBEDDING_DIMENSIONS"):
        monkeypatch.delenv(name, raising=False)
    with _reader(tmp_path, "schema7_visible", "project", revision=revision, schema=schema), closing(
        sqlite3.connect(tmp_path / "ghost.sqlite3"),
    ) as old:
        old.row_factory = _row_factory
        old.execute("PRAGMA foreign_keys=ON")
        old.execute("UPDATE sqlite_sequence SET seq=101 WHERE name='observation_events'")
        embeddings = _Embeddings()
        row: dict[str, object] = {"user_text": "archivedquartz", "assistant_text": "Recorded."}
        old.execute(
            "INSERT INTO memory_embeddings(run_id,model_id,dimensions,content_digest,vector) VALUES(?,?,?,?,?)",
            ("archived-run", embeddings.model_id, embeddings.dimensions, observation_digest(row),
             pack_embedding((1.0, 0.0), 2)),
        )
        old.commit()
        history = _rows(old, "SELECT * FROM observation_events ORDER BY sequence")
        vectors = _rows(old, "SELECT * FROM memory_embeddings")
        retired = _rows(old, "SELECT * FROM ghost_retired_inputs ORDER BY kind,identity")
        if concurrent:
            with ThreadPoolExecutor(max_workers=4) as executor:
                list(executor.map(_open, (tmp_path,) * 4))
        else:
            _open(tmp_path)
        store = GhostObservationStore(tmp_path, embeddings=embeddings)
        with store.database.connection() as database:
            assert _rows(database, "SELECT * FROM observation_history ORDER BY sequence") == history
            assert _rows(database, "SELECT * FROM memory_embeddings") == vectors
            assert _rows(database, "SELECT * FROM ghost_retired_inputs ORDER BY kind,identity") == retired
            assert _rows(database, "PRAGMA foreign_key_check") == ()
            assert _rows(database, "PRAGMA foreign_key_list(memory_embeddings)")[0][2] == "observation_history"
            assert _rows(database, "PRAGMA user_version") == ((GHOST_SCHEMA_VERSION,),)
            assert event_window(database, "observations") == 0
        assert _embedding_ids(store) == ("archived-run",)
        assert store.retrieve("archivedquartz", scope="project", project=str(tmp_path / "project"))
        assert store.append_completed(
            run_id="new-run", session_id="new-session", mode="chat", user_text="New fact",
            assistant_text="Recorded.", stop_reason="done",
        )
        with store.database.connection() as database:
            assert _rows(database, "SELECT sequence FROM observation_history WHERE run_id='new-run'") == ((102,),)


@pytest.mark.parametrize(("revision", "schema"), LEGACY_LAYOUTS)
def test_failed_migration_rolls_back_table_rename_ddl_and_version(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, revision: str, schema: int,
) -> None:
    for name in ("GHOST_EMBEDDING_URL", "GHOST_EMBEDDING_MODEL", "GHOST_EMBEDDING_DIMENSIONS"):
        monkeypatch.delenv(name, raising=False)
    with _reader(tmp_path, "schema7_visible", "project", revision=revision, schema=schema), closing(
        sqlite3.connect(tmp_path / "ghost.sqlite3"),
    ) as old:
        old.row_factory = _row_factory
        before_schema = _rows(old, "SELECT type,name,tbl_name,sql FROM sqlite_master ORDER BY type,name")
        before_history = _rows(old, "SELECT * FROM observation_events ORDER BY sequence")
        execute = sqlite_module._execute_schema

        def fail_after_ddl(database: sqlite3.Connection, script: str) -> None:
            execute(database, script)
            raise sqlite3.OperationalError("injected migration failure")

        with monkeypatch.context() as patch:
            patch.setattr(sqlite_module, "_execute_schema", fail_after_ddl)
            with pytest.raises(sqlite3.OperationalError, match="injected migration failure"):
                _open(tmp_path)
        assert _rows(old, "SELECT type,name,tbl_name,sql FROM sqlite_master ORDER BY type,name") == before_schema
        assert _rows(old, "SELECT * FROM observation_events ORDER BY sequence") == before_history
        assert _rows(old, "PRAGMA user_version") == ((schema,),)
        _open(tmp_path)
        assert _rows(old, "SELECT * FROM observation_history ORDER BY sequence") == before_history


@pytest.mark.parametrize(("revision", "schema"), LEGACY_LAYOUTS)
def test_old_cached_select_reparses_against_bounded_view_without_retiring_other_sources(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, revision: str, schema: int,
) -> None:
    for name in ("GHOST_EMBEDDING_URL", "GHOST_EMBEDDING_MODEL", "GHOST_EMBEDDING_DIMENSIONS"):
        monkeypatch.delenv(name, raising=False)
    with _reader(tmp_path, "schema7_visible", "project", revision=revision, schema=schema), closing(
        sqlite3.connect(tmp_path / "ghost.sqlite3"),
    ) as old:
        old.row_factory = _row_factory
        query = "SELECT run_id FROM observation_events WHERE run_id='archived-run'"
        assert _rows(old, query) == (("archived-run",),)
        store = GhostObservationStore(tmp_path)
        _advance_observations(store, str(tmp_path / "other-project"))
        assert _rows(old, query) == ()
        with store.database.connection() as database:
            assert _rows(database, "SELECT run_id FROM observation_history WHERE run_id='archived-run'")
            assert _rows(database, "SELECT * FROM ghost_retired_inputs") == ()
            assert _rows(database, "SELECT count(*) FROM observation_events") == ((5000,),)


@pytest.mark.parametrize("damage", ["missing", "unacknowledged", "anchor"])
def test_plain_legacy_view_fails_closed_on_invalid_window(tmp_path: Path, damage: str) -> None:
    store = GhostObservationStore(tmp_path)
    assert store.append_completed(
        run_id="run", session_id="session", mode="chat", user_text="Fact",
        assistant_text="Recorded.", stop_reason="done",
    )
    with store.database.write_transaction() as database:
        if damage == "missing":
            database.execute("DELETE FROM ghost_replay_windows WHERE stream='observations'")
        elif damage == "unacknowledged":
            database.execute("UPDATE ghost_replay_windows SET after_sequence=1 WHERE stream='observations'")
        else:
            database.execute(
                "UPDATE ghost_replay_windows SET after_sequence=10,applied_after_sequence=10 WHERE stream='observations'",
            )
    with closing(sqlite3.connect(store.database_path)) as plain:
        plain.row_factory = _row_factory
        assert _rows(plain, "SELECT * FROM observation_events") == ()
        assert len(_rows(plain, "SELECT * FROM observation_history")) == 1
    with pytest.raises(ValueError, match="window|anchor"):
        store.read_committed(scope="user")


@pytest.mark.parametrize("relation", ["history", "view"])
def test_existing_schema_nine_missing_relation_is_not_silently_recreated(tmp_path: Path, relation: str) -> None:
    owner = GhostSQLite(tmp_path)
    with owner.write_transaction() as database:
        if relation == "history":
            database.execute("DROP TABLE observation_history")
        else:
            database.execute("DROP VIEW observation_events")
    with pytest.raises(GhostSchemaVersionError, match="history or consumption view"):
        GhostSQLite(tmp_path)


@pytest.mark.parametrize("operation", ["insert", "update", "delete"])
@pytest.mark.parametrize("empty", [False, True])
def test_native_readonly_view_rejects_even_zero_row_legacy_mutations(
    tmp_path: Path, operation: str, empty: bool,
) -> None:
    store = GhostObservationStore(tmp_path)
    assert store.append_completed(
        run_id="run", session_id="session", mode="chat", user_text="Fact",
        assistant_text="Recorded.", stop_reason="done",
    )
    with store.database.connection() as database:
        before = _rows(database, "SELECT * FROM observation_history ORDER BY sequence")
    predicate = "0" if empty else "1"
    statements = {
        "insert": f"INSERT INTO observation_events SELECT * FROM observation_history WHERE {predicate}",
        "update": f"UPDATE observation_events SET user_text='Replacement' WHERE {predicate}",
        "delete": f"DELETE FROM observation_events WHERE {predicate}",
    }
    with closing(sqlite3.connect(store.database_path)) as plain, pytest.raises(
        sqlite3.OperationalError, match="observation_events because it is a view",
    ):
        plain.execute(statements[operation])
    with store.database.connection() as database:
        assert _rows(database, "SELECT * FROM observation_history ORDER BY sequence") == before
