"""Physical deletion guards also protect older cached SQLite connections."""

from __future__ import annotations

import sqlite3
from contextlib import closing
from pathlib import Path

import pytest

from codey.ghost.control_surface import GhostControlSurface
from codey.ghost.sqlite import _row_factory
from tests.test_ghost_mixed_schema_history import _finish_worker, _legacy_worker
from tests.test_ghost_reset_boundaries import _state
from tests.test_ghost_scope_history_retention import _seed_history, _visible

TABLES = ("memory_events", "graph_events", "observation_history")
STORES = {"memory_events": "inbox", "graph_events": "hebbian", "observation_history": "observations"}


@pytest.mark.parametrize("table", TABLES)
@pytest.mark.parametrize("plain_connection", [False, True])
def test_physical_delete_is_rejected_and_prior_projection_mutations_roll_back(
    tmp_path: Path, table: str, plain_connection: bool,
) -> None:
    surface = _seed_history(tmp_path, "project")
    assert surface.observations is not None
    before = _state(surface)
    assert table in TABLES and before[table]
    owner = surface.observations.database
    connection = sqlite3.connect(owner.path) if plain_connection else owner._connect()
    with closing(connection), pytest.raises(sqlite3.IntegrityError, match="Ghost original history is retained"), connection:
        connection.execute("DELETE FROM memory_records WHERE collection='inbox'")
        connection.execute(f"DELETE FROM {table}")
    assert _state(surface) == before
    assert _visible(surface, STORES[table])


@pytest.mark.parametrize("table", TABLES)
@pytest.mark.parametrize("mode", ["cached_statement", "read_snapshot"])
def test_connection_opened_before_real_schema_upgrade_cannot_delete_history(
    tmp_path: Path, table: str, mode: str,
) -> None:
    assert table in TABLES
    with _legacy_worker(tmp_path, STORES[table], "reset") as worker, closing(
        sqlite3.connect(tmp_path / "ghost.sqlite3"),
    ) as old:
        old.row_factory = _row_factory
        legacy_table = "observation_events" if table == "observation_history" else table
        delete = f"DELETE FROM {legacy_table} WHERE sequence=?"
        if mode == "cached_statement":
            old.execute(delete, (-1,))
            old.commit()
        else:
            old.execute("BEGIN")
        first: sqlite3.Row | None = old.execute(f"SELECT sequence FROM {legacy_table} ORDER BY sequence LIMIT 1").fetchone()
        assert first is not None
        sequence: object = first[0]
        assert type(sequence) is int
        surface = GhostControlSurface.from_state_home(tmp_path)
        before = _state(surface)
        with pytest.raises(sqlite3.Error) as failure:
            old.execute(delete, (sequence,))
        if mode == "cached_statement":
            if table == "observation_history":
                assert isinstance(failure.value, sqlite3.OperationalError)
                assert "observation_events because it is a view" in str(failure.value)
            else:
                assert isinstance(failure.value, sqlite3.IntegrityError)
                assert "Ghost original history is retained" in str(failure.value)
        else:
            assert failure.value.sqlite_errorcode == sqlite3.SQLITE_BUSY_SNAPSHOT
        old.rollback()
        assert _state(surface) == before
        assert _finish_worker(worker) == "rejected"
        assert _state(surface) == before
        assert _visible(surface, STORES[table])


def test_existing_installation_adds_guards_without_changing_history_or_windows(tmp_path: Path) -> None:
    surface = _seed_history(tmp_path, "project")
    assert surface.observations is not None
    before = _state(surface)
    # Guard repair is independent of the current history table's schema name.
    with surface.observations.database.connection() as database:
        for table in TABLES:
            guard = "observation_events" if table == "observation_history" else table
            database.execute(f"DROP TRIGGER ghost_preserve_{guard}")
    reopened = GhostControlSurface.from_state_home(tmp_path)
    assert _state(reopened) == before
    assert reopened.observations is not None
    for table in TABLES:
        with pytest.raises(sqlite3.IntegrityError, match="Ghost original history is retained"), (
            reopened.observations.database.write_transaction()
        ) as database:
            database.execute(f"DELETE FROM {table}")
