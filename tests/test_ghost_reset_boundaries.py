"""Reset retains history while preserving recovery, retirement and new input."""

from __future__ import annotations

import sqlite3
from collections.abc import Iterable
from pathlib import Path

import pytest

from codey.ghost.control_surface import GhostControlSurface
from codey.ghost.observations import MAX_OBSERVATIONS, GhostObservationStore
from tests.manual.ghost_memory_routing.deletion_runtime_probe import _seed
from tests.test_ghost_capacity_history_retention import _owner, _prepare_pressure
from tests.test_ghost_scope_history_retention import (
    NOW,
    STORES,
    _embedding_ids,
    _Embeddings,
    _history,
    _rebuild,
    _seed_history,
    _visible,
)

TABLES = (
    "memory_events", "memory_records", "memory_consumers", "ghost_settings",
    "graph_events", "graph_nodes", "graph_edges", "graph_consumers",
    "observation_history", "memory_embeddings", "ghost_input_origins",
    "ghost_retired_inputs", "ghost_retired_sources", "ghost_projection_epoch", "ghost_replay_windows",
)


def _reset(surface: GhostControlSurface, identity: str = "boundary-reset") -> None:
    status, payload = surface.dispatch_action({
        "action": "reset_all", "confirm": True, "delete_operation_id": identity,
    })
    assert status == 200, payload


def _state(surface: GhostControlSurface) -> dict[str, tuple[tuple[object, ...], ...]]:
    assert surface.observations is not None
    state: dict[str, tuple[tuple[object, ...], ...]] = {}
    with surface.observations.database.connection() as database:
        for table in TABLES:
            rows: Iterable[sqlite3.Row] = database.execute(f'SELECT * FROM "{table}" ORDER BY rowid')
            values: list[tuple[object, ...]] = []
            for row in rows:
                fields: list[object] = []
                for index in range(len(row)):
                    value: object = row[index]
                    fields.append(value)
                values.append(tuple(fields))
            state[table] = tuple(values)
    return state


@pytest.mark.parametrize("store", STORES)
@pytest.mark.parametrize("damage", ["missing", "anchor", "unacknowledged"])
def test_explicit_reset_recovers_window_metadata_without_erasing_original_history(
    tmp_path: Path, store: str, damage: str,
) -> None:
    surface = _seed_history(tmp_path, "project")
    assert surface.observations is not None
    original = _history(surface, store)
    with surface.observations.database.connection() as database:
        if damage == "missing":
            database.execute("DELETE FROM ghost_replay_windows WHERE stream=?", (store,))
        elif damage == "anchor":
            database.execute(
                "UPDATE ghost_replay_windows SET after_sequence=1000000000,applied_after_sequence=1000000000 "
                "WHERE stream=?", (store,),
            )
        else:
            database.execute(
                "UPDATE ghost_replay_windows SET applied_after_sequence=1 WHERE stream=?", (store,),
            )
    _reset(surface)
    reopened = GhostControlSurface.from_state_home(tmp_path)
    _rebuild(reopened)
    assert not _visible(reopened, store)
    assert _history(reopened, store) == original


@pytest.mark.parametrize("store", tuple(name for name in STORES if name != "observations"))
def test_explicit_reset_recovers_a_damaged_consumer_cursor_without_erasing_events(tmp_path: Path, store: str) -> None:
    surface = _seed_history(tmp_path, "project")
    assert surface.observations is not None
    original = _history(surface, store)
    with surface.observations.database.connection() as database:
        table, key = ("graph_consumers", "graph") if store in {"hebbian", "affinity"} else (
            "memory_consumers", "collection",
        )
        database.execute(f"UPDATE {table} SET applied_sequence=1000000000 WHERE {key}=?", (store,))
    _reset(surface)
    reopened = GhostControlSurface.from_state_home(tmp_path)
    _rebuild(reopened)
    assert not _visible(reopened, store)
    assert _history(reopened, store) == original


@pytest.mark.parametrize("store", STORES)
def test_window_write_failure_rolls_back_reset_state_and_retirement_together(tmp_path: Path, store: str) -> None:
    surface = _seed_history(tmp_path, "project")
    assert surface.observations is not None
    before = _state(surface)
    assert store in STORES
    with surface.observations.database.connection() as database:
        database.execute(
            "CREATE TRIGGER reject_reset_window BEFORE UPDATE ON ghost_replay_windows "
            f"WHEN NEW.stream='{store}' BEGIN SELECT RAISE(ABORT,'reset window failure'); END",
        )
    with pytest.raises(sqlite3.IntegrityError, match="reset window failure"):
        if store == "observations":
            surface.observations.reset_all()
        else:
            _owner(surface, store).reset()
    assert _state(surface) == before
    assert _visible(surface, store)


@pytest.mark.parametrize("store", STORES)
def test_reset_preserves_original_history_above_actual_capacity(tmp_path: Path, store: str) -> None:
    surface = _seed_history(tmp_path, "project")
    assert surface.observations is not None
    if store == "observations":
        with surface.observations.database.write_transaction() as database:
            database.executemany(
                "INSERT INTO observation_history(run_id,session_id,project,mode,user_text,assistant_text,"
                "stop_reason,committed,provider_id,ts) VALUES(?,?,?,?,?,?,?,?,?,?)",
                (
                    (f"capacity-{index:06d}", "session", str(tmp_path / "project"), "chat", "Capacity fact.",
                     "Recorded.", "done", 1, "fixture", NOW)
                    for index in range(MAX_OBSERVATIONS)
                ),
            )
    else:
        _prepare_pressure(surface, store, True)
    original = _history(surface, store)
    assert original and _visible(surface, store)
    _reset(surface)
    reopened = GhostControlSurface.from_state_home(tmp_path)
    _rebuild(reopened)
    assert not _visible(reopened, store)
    assert _history(reopened, store) == original


@pytest.mark.parametrize("scope", ["user", "project", "session"])
def test_completed_reset_retry_keeps_new_input_and_new_reset_retains_both_histories(tmp_path: Path, scope: str) -> None:
    surface = _seed_history(tmp_path, scope)
    original = {store: _history(surface, store) for store in STORES}
    _reset(surface)
    fresh = _seed(surface, str(tmp_path / "project"), scope=scope, run_id="fresh-run")
    assert surface.inbox is not None and surface.observations is not None
    before_retry = _state(surface)
    _reset(surface)
    assert _state(surface) == before_retry, "completed reset ran twice and changed later input"
    assert any(candidate.id == fresh.id for candidate in surface.inbox.list_candidates())
    assert [row["run_id"] for row in surface.observations.read_all()] == ["fresh-run"]
    before_second_reset = {store: _history(surface, store) for store in STORES}
    _reset(surface, "new-reset")
    reopened = GhostControlSurface.from_state_home(tmp_path)
    _rebuild(reopened)
    for store in STORES:
        assert not _visible(reopened, store)
        assert _history(reopened, store) == before_second_reset[store]
        assert _history(reopened, store)[:len(original[store])] == original[store]
    for run_id in ("old-run", "fresh-run"):
        assert not surface.observations.append_completed(
            run_id=run_id, session_id="session", mode="chat", user_text="Retired input",
            assistant_text="Retired output", stop_reason="done",
        )


@pytest.mark.parametrize("control", ["accept_events", "retrieve_projections", "admit_hints", "run_maintenance"])
def test_explicit_reset_still_runs_while_preserving_the_disabled_control(tmp_path: Path, control: str) -> None:
    surface = _seed_history(tmp_path, "project")
    assert surface.inbox is not None
    assert surface.inbox.set_control(control, False)
    original = _history(surface, "inbox")
    _reset(surface)
    reopened = GhostControlSurface.from_state_home(tmp_path)
    assert reopened.inbox is not None and not reopened.inbox.control_enabled(control)
    assert not reopened.inbox.list_candidates()
    assert _history(reopened, "inbox") == original


@pytest.mark.parametrize("window", ["index", "query"])
def test_reset_during_embedding_keeps_raw_facts_without_returning_or_recaching_old_input(
    tmp_path: Path, window: str,
) -> None:
    embedder = _Embeddings()
    observations = GhostObservationStore(tmp_path, embeddings=embedder)
    assert observations.append_completed(
        run_id="old-run", session_id="session", mode="chat", user_text="Recovery check",
        assistant_text="Needs review", stop_reason="done",
    )
    if window == "query":
        assert observations.retrieve("Recovery", session_id="session")
        assert _embedding_ids(observations) == ("old-run",)
    surface = GhostControlSurface.from_state_home(tmp_path)
    original = _history(surface, "observations")

    def reset() -> None:
        _reset(surface)

    embedder.before_encode = reset
    assert not observations.retrieve("Recovery", session_id="session")
    assert not _embedding_ids(observations)
    assert _history(surface, "observations") == original
    assert observations.append_completed(
        run_id="fresh-run", session_id="session", mode="chat", user_text="New recovery question",
        assistant_text="New review", stop_reason="done",
    )
    assert [row["run_id"] for row in observations.retrieve("Recovery", session_id="session")] == ["fresh-run"]
    assert _embedding_ids(observations) == ("fresh-run",)
    assert _history(surface, "observations")[:len(original)] == original
