"""Replay windows keep bounded checkpoints separate from immutable audit rows."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from codey.ghost import vectors as vectors_module
from codey.ghost.control_surface import GhostControlSurface
from codey.ghost.event_windows import EventStream, event_window
from codey.ghost.graph_store import GhostGraph
from codey.ghost.observations import MAX_OBSERVATIONS, GhostObservationStore
from codey.ghost.record_store import GhostRecords
from codey.ghost.sleep import MAX_SLEEP_EVENTS_BYTES
from codey.ghost.sqlite import GHOST_SCHEMA_VERSION, GhostSchemaVersionError, GhostSQLite
from tests.test_ghost_capacity_history_retention import EVENT_LIMITS, _owner, _prepare_pressure
from tests.test_ghost_scope_history_retention import (
    NOW,
    _embedding_ids,
    _Embeddings,
    _history,
    _rebuild,
    _seed_history,
    _visible,
)


def _stream(owner: GhostRecords | GhostGraph) -> EventStream:
    return owner.name if isinstance(owner, GhostGraph) else owner.collection


def _checkpoint(owner: GhostRecords | GhostGraph, *, empty: bool = False) -> None:
    with owner.transaction():
        events = [] if empty else owner.events()
        if isinstance(owner, GhostGraph):
            nodes, edges = owner.rows()
            owner.append(events, replace=True)
            owner.replace_rows(() if empty else nodes, () if empty else edges)
        else:
            rows = owner.rows()
            owner.append(events, replace=True)
            owner.replace_rows(() if empty else rows)


@pytest.mark.parametrize("store", tuple(EVENT_LIMITS))
def test_repeated_checkpoint_bounds_replay_without_rewriting_prior_events(tmp_path: Path, store: str) -> None:
    surface = _seed_history(tmp_path, "project")
    owner = _owner(surface, store)
    original = _history(surface, store)
    active_count = len(owner.events())
    for _iteration in range(3):
        _checkpoint(owner)
        assert len(owner.events()) == active_count
        assert owner.event_stats()["events"] == active_count
        assert _history(surface, store)[:len(original)] == original
    assert len(_history(surface, store)) == len(original) + 3 * active_count
    reopened = GhostControlSurface.from_state_home(tmp_path)
    _rebuild(reopened)
    assert _visible(reopened, store)


@pytest.mark.parametrize("store", tuple(EVENT_LIMITS))
def test_real_sleep_maintenance_keeps_event_count_within_existing_capacity(tmp_path: Path, store: str) -> None:
    surface = _seed_history(tmp_path, "project")
    _prepare_pressure(surface, store, True)
    before = _history(surface, store)
    assert surface.sleep is not None
    report = surface.sleep.run_once(
        inbox_store=surface.inbox, hebbian_store=surface.hebbian, continuity_store=surface.continuity,
        work_queue_store=surface.work_queue, affinity_store=surface.affinity,
        session_id="session", project=str(tmp_path / "project"),
    )
    assert all(step.ok for step in report.steps)
    owner = _owner(surface, store)
    assert owner.event_stats()["events"] <= EVENT_LIMITS[store]
    assert _history(surface, store)[:len(before)] == before


@pytest.mark.parametrize("scope", ["user", "project", "session"])
def test_sleep_report_window_obeys_byte_budget_without_copying_old_reports(tmp_path: Path, scope: str) -> None:
    surface = _seed_history(tmp_path, scope)
    _prepare_pressure(surface, "sleep", True)
    assert surface.sleep is not None
    report = surface.sleep.run_once()
    assert all(step.ok for step in report.steps)
    assert surface.sleep.records.event_stats()["bytes"] <= MAX_SLEEP_EVENTS_BYTES
    before = len(_history(surface, "sleep"))
    assert all(step.ok for step in surface.sleep.run_once().steps)
    assert len(_history(surface, "sleep")) == before + 1
    assert surface.sleep.records.event_stats()["bytes"] <= MAX_SLEEP_EVENTS_BYTES


@pytest.mark.parametrize(("max_events", "max_bytes"), [(0, 1), (1, 0), (1, 1)])
def test_invalid_report_tail_capacity_preserves_history_and_current_projection(
    tmp_path: Path, max_events: int, max_bytes: int,
) -> None:
    surface = _seed_history(tmp_path, "project")
    assert surface.sleep is not None
    before = _history(surface, "sleep")
    active = surface.sleep.records.events()
    with pytest.raises(ValueError):
        surface.sleep.records.retain_recent_events(max_events=max_events, max_bytes=max_bytes)
    assert _history(surface, "sleep") == before
    assert surface.sleep.records.events() == active
    assert _visible(surface, "sleep")


def test_generic_source_projection_cannot_be_replaced_by_a_report_tail(tmp_path: Path) -> None:
    surface = _seed_history(tmp_path, "project")
    assert surface.inbox is not None
    before = _history(surface, "inbox")
    with pytest.raises(ValueError, match="only Sleep"):
        surface.inbox.records.retain_recent_events(max_events=1, max_bytes=1024)
    assert _history(surface, "inbox") == before
    assert _visible(surface, "inbox")


@pytest.mark.parametrize("store", tuple(EVENT_LIMITS))
def test_empty_checkpoint_does_not_replay_old_state(tmp_path: Path, store: str) -> None:
    surface = _seed_history(tmp_path, "project")
    owner = _owner(surface, store)
    before = _history(surface, store)
    _checkpoint(owner, empty=True)
    assert owner.events() == []
    assert owner.event_stats() == {"events": 0, "bytes": 0}
    assert _history(surface, store) == before
    reopened = GhostControlSurface.from_state_home(tmp_path)
    _rebuild(reopened)
    assert not _visible(reopened, store)


@pytest.mark.parametrize("store", tuple(EVENT_LIMITS))
def test_checkpoint_window_and_history_rollback_together(tmp_path: Path, store: str) -> None:
    surface = _seed_history(tmp_path, "project")
    owner = _owner(surface, store)
    before = _history(surface, store)
    with owner.database.connection() as database:
        boundary = event_window(database, _stream(owner))
    with pytest.raises(RuntimeError, match="checkpoint failure"), owner.transaction():
        _checkpoint(owner)
        raise RuntimeError("checkpoint failure")
    assert _history(surface, store) == before
    with owner.database.connection() as database:
        assert event_window(database, _stream(owner)) == boundary
    assert _visible(surface, store)


@pytest.mark.parametrize("store", tuple(EVENT_LIMITS))
def test_empty_checkpoint_requires_projection_acknowledgement(tmp_path: Path, store: str) -> None:
    surface = _seed_history(tmp_path, "project")
    owner = _owner(surface, store)
    original = _history(surface, store)
    active = owner.events()
    with pytest.raises(ValueError, match="replay window"), owner.transaction():
        owner.append((), replace=True)
    assert owner.events() == active
    assert _history(surface, store) == original
    assert _visible(surface, store)


@pytest.mark.parametrize("store", tuple(EVENT_LIMITS))
@pytest.mark.parametrize("damage", ["missing", "anchor"])
def test_damaged_window_refuses_event_and_projection_reads(tmp_path: Path, store: str, damage: str) -> None:
    surface = _seed_history(tmp_path, "project")
    owner = _owner(surface, store)
    with owner.database.connection() as database:
        if damage == "missing":
            database.execute("DELETE FROM ghost_replay_windows WHERE stream=?", (_stream(owner),))
        else:
            database.execute(
                "UPDATE ghost_replay_windows SET after_sequence=1000000000,applied_after_sequence=1000000000 "
                "WHERE stream=?", (_stream(owner),),
            )
    with pytest.raises(ValueError, match="replay"):
        owner.events()
    with pytest.raises(ValueError, match="replay"):
        owner.rows()
    if damage == "missing":
        with pytest.raises(GhostSchemaVersionError, match="replay windows"):
            GhostSQLite(tmp_path)


def test_schema_seven_upgrade_preserves_all_existing_events_and_initializes_windows(tmp_path: Path) -> None:
    surface = _seed_history(tmp_path, "project")
    before = {store: _history(surface, store) for store in (*EVENT_LIMITS, "observations")}
    assert surface.observations is not None
    with surface.observations.database.connection() as database:
        database.execute("DROP TABLE ghost_replay_windows")
        database.execute("PRAGMA user_version=7")
    reopened = GhostControlSurface.from_state_home(tmp_path)
    for store, history in before.items():
        assert _history(reopened, store) == history
        assert _visible(reopened, store)
    assert reopened.observations is not None
    with reopened.observations.database.connection() as database:
        streams: tuple[EventStream, ...] = (
            "inbox", "hebbian", "continuity", "work_queue", "affinity", "sleep", "observations",
        )
        for stream in streams:
            assert event_window(database, stream) == 0
        row: sqlite3.Row | None = database.execute("PRAGMA user_version").fetchone()
        assert row is not None
        version: object = row[0]
        assert version == GHOST_SCHEMA_VERSION == 9


def _advance_observations(store: GhostObservationStore, project: str) -> None:
    with store.database.write_transaction() as database:
        database.executemany(
            "INSERT INTO observation_history(run_id,session_id,project,mode,user_text,assistant_text,"
            "stop_reason,committed,provider_id,ts) VALUES(?,?,?,?,?,?,?,?,?,?)",
            (
                (f"window-{index:06d}", "other", project, "chat", "Newer fact.", "Recorded.", "done", 1, "fixture", NOW)
                for index in range(MAX_OBSERVATIONS)
            ),
        )
    assert store.append_completed(
        run_id="newest-window-run", session_id="other", project=project, mode="chat",
        user_text="New completion.", assistant_text="Recorded.", stop_reason="done",
    )


def test_observation_window_stays_bounded_and_does_not_backfill_an_archived_scope(tmp_path: Path) -> None:
    surface = _seed_history(tmp_path, "project")
    assert surface.observations is not None
    store = surface.observations
    _advance_observations(store, str(tmp_path / "other"))
    assert len(_history(surface, "observations")) == MAX_OBSERVATIONS + 2
    assert len(store.read_all()) == MAX_OBSERVATIONS
    assert store.read_committed(scope="project", project=str(tmp_path / "project")) == ()
    assert store.delete_scope("project", project=str(tmp_path / "other")) == MAX_OBSERVATIONS
    assert store.read_all() == ()
    assert store.read_committed(scope="project", project=str(tmp_path / "project")) == ()
    assert len(_history(surface, "observations")) == MAX_OBSERVATIONS + 2
    assert store.delete_scope("project", project=str(tmp_path / "project")) == 0
    assert not store.append_completed(
        run_id="old-run", session_id="session", project=str(tmp_path / "project"), mode="chat",
        user_text="Old source.", assistant_text="Old output.", stop_reason="done",
    )


@pytest.mark.parametrize("phase", ["index", "query"])
def test_capacity_advance_during_embedding_cannot_reindex_or_return_archived_facts(
    tmp_path: Path, phase: str,
) -> None:
    surface = _seed_history(tmp_path, "project")
    embeddings = _Embeddings()
    store = GhostObservationStore(tmp_path, embeddings=embeddings)
    if phase == "query":
        assert store.retrieve("concise", scope="user")
        assert _embedding_ids(store) == ("old-run",)

    def advance() -> None:
        _advance_observations(store, str(tmp_path / "other"))

    embeddings.before_encode = advance
    assert store.retrieve("concise", scope="user") == ()
    assert _embedding_ids(store) == ()
    assert len(_history(surface, "observations")) == MAX_OBSERVATIONS + 2


@pytest.mark.parametrize("phase", ["index", "query"])
def test_capacity_move_after_window_read_is_rechecked_by_vector_sql(
    tmp_path: Path, phase: str, monkeypatch: pytest.MonkeyPatch,
) -> None:
    surface = _seed_history(tmp_path, "project")
    store = GhostObservationStore(tmp_path, embeddings=_Embeddings())
    if phase == "query":
        assert store.retrieve("concise", scope="user")
    original = event_window
    advanced = False

    def interleave(connection: sqlite3.Connection, stream: EventStream) -> int:
        nonlocal advanced
        boundary = original(connection, stream)
        if not advanced:
            advanced = True
            _advance_observations(store, str(tmp_path / "other"))
        return boundary

    monkeypatch.setattr(vectors_module, "event_window", interleave)
    assert store.retrieve("concise", scope="user") == ()
    assert advanced
    assert _embedding_ids(store) == ()
    assert len(_history(surface, "observations")) == MAX_OBSERVATIONS + 2
