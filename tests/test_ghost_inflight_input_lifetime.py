"""Deleted input identities stay deleted before any projection exists."""

from __future__ import annotations

import json
import multiprocessing
import sqlite3
from contextlib import closing
from multiprocessing.queues import Queue
from multiprocessing.synchronize import Event
from pathlib import Path

import pytest

from codey.app.context import AppContext
from codey.ghost.control_surface import GhostControlSurface
from codey.ghost.observations import GhostObservationStore
from codey.ghost.sqlite import GHOST_SCHEMA_VERSION, GhostSchemaVersionError, GhostSQLite
from codey.runs.ledger_projection import ProviderFailureSummary, RunLedgerProjection
from codey.runs.work_checkpoint import WorkCheckpointStore
from codey.task.model import TaskSubmission
from tests.manual.ghost_memory_routing.inflight_delete_probe import cell


@pytest.mark.parametrize("action", ["delete_scope", "reset_all"])
@pytest.mark.parametrize("enabled", [True, False])
def test_runtime_deletion_before_first_projection_does_not_reconsume_input(
    tmp_path: Path, action: str, enabled: bool,
) -> None:
    result = cell(tmp_path, action, enabled, 0)
    assert result.empty_before_delete and result.delete_ok and result.task_completed
    assert result.observation_rows == 0
    assert result.continuity_rows == 0


def _register(surface: GhostControlSurface, project: str) -> None:
    assert surface.observations is not None
    assert surface.observations.register_input(run_id="old-run", session_id="old-session", project=project)


def _delete(surface: GhostControlSurface, action: str, scope: str, project: str) -> None:
    status, payload = surface.dispatch_action({
        "action": action, "scope": scope, "project": project, "session_id": "old-session", "confirm": True,
    })
    assert status == 200 and payload.get("ok") is True


def _append(store: GhostObservationStore, run_id: str, *, session_id: str = "old-session", project: str = "") -> bool:
    return store.append_completed(
        run_id=run_id, session_id=session_id, project=project, mode="chat", user_text="old input",
        assistant_text="completed reply", stop_reason="done",
    )


@pytest.mark.parametrize("action,scope", [
    ("delete_scope", "user"), ("delete_scope", "project"), ("delete_scope", "session"), ("reset_all", "user"),
])
def test_unprojected_identity_retirement_survives_reopen_and_repeated_delete(
    tmp_path: Path, action: str, scope: str,
) -> None:
    project = str(tmp_path / "project")
    surface = GhostControlSurface.from_state_home(tmp_path)
    _register(surface, project)
    _delete(surface, action, scope, project)
    surface = GhostControlSurface.from_state_home(tmp_path)
    _delete(surface, action, scope, project)
    assert surface.observations is not None
    assert not surface.observations.register_input(run_id="old-run", session_id="old-session", project=project)
    assert not _append(surface.observations, "old-run", project=project)
    assert surface.observations.register_input(run_id="new-run", session_id="old-session", project=project)
    assert _append(surface.observations, "new-run", project=project)
    assert len(surface.observations.read_all()) == 1


@pytest.mark.parametrize("scope", ["session", "project"])
def test_input_delete_preserves_other_scope_identities(tmp_path: Path, scope: str) -> None:
    surface = GhostControlSurface.from_state_home(tmp_path)
    project = str(tmp_path / "project")
    _register(surface, project)
    assert surface.observations is not None
    other_session = "other-session" if scope == "session" else "old-session"
    other_project = str(tmp_path / "other-project") if scope == "project" else project
    assert surface.observations.register_input(run_id="other-run", session_id=other_session, project=other_project)
    _delete(surface, "delete_scope", scope, project)
    assert _append(surface.observations, "other-run", session_id=other_session, project=other_project)
    assert not _append(surface.observations, "old-run", project=project)


@pytest.mark.parametrize("consumer", ["continuity", "affinity", "work_queue", "sleep"])
def test_fresh_consumers_reject_deleted_unprojected_run(tmp_path: Path, consumer: str) -> None:
    project = str(tmp_path / "project")
    surface = GhostControlSurface.from_state_home(tmp_path)
    _register(surface, project)
    _delete(surface, "delete_scope", "session", project)
    # A new store instance starts after deletion; a thread-local generation
    # from the original task cannot protect this fresh read.
    surface = GhostControlSurface.from_state_home(tmp_path)
    assert surface.continuity is not None and surface.work_queue is not None
    assert surface.affinity is not None and surface.sleep is not None
    projection = RunLedgerProjection(
        run_id="old-run", session_id="old-session", project=project, mode="chat",
        provider_failures=(ProviderFailureSummary("local", "send", "timeout", "transport", "fixture failure"),),
    )
    if consumer == "continuity":
        surface.continuity.sync_from_sources(run_projection=projection, session_id="old-session", project=project)
    elif consumer == "affinity":
        surface.affinity.sync_from_sources(run_projection=projection, session_id="old-session", project=project)
    elif consumer == "work_queue":
        surface.work_queue.sync_from_sources(run_projection=projection, session_id="old-session", project=project)
    else:
        surface.sleep.run_once(
            inbox_store=surface.inbox, continuity_store=surface.continuity, affinity_store=surface.affinity,
            run_projection=projection, session_id="old-session", project=project,
        )
    assert not surface.continuity.list_items()
    assert not surface.work_queue.list_items()
    assert not surface.affinity.list_nodes()


@pytest.mark.parametrize("collection", ["continuity", "work_queue"])
@pytest.mark.parametrize("source", ["task_done", "run_ledger", "work_checkpoint", "review", "continuity"])
@pytest.mark.parametrize("mutation", ["insert", "update"])
def test_sql_writers_cannot_reuse_retired_run_sources(
    tmp_path: Path, collection: str, source: str, mutation: str,
) -> None:
    surface = GhostControlSurface.from_state_home(tmp_path)
    _register(surface, "")
    _delete(surface, "delete_scope", "session", "")
    assert surface.observations is not None
    database = surface.observations.database
    with database.connection() as connection:
        if mutation == "update":
            connection.execute(
                "INSERT INTO memory_records(collection,id,scope,scope_ref,status,payload) VALUES(?,?,?,?,?,?)",
                (collection, "fresh-record", "session", "old-session", "active", "{}"),
            )
    with pytest.raises(sqlite3.IntegrityError, match="deleted Ghost run source"), database.connection() as connection:
        payload = json.dumps({"source": source, "source_ref": "old-run"})
        if mutation == "insert":
            connection.execute(
                "INSERT INTO memory_records(collection,id,scope,scope_ref,status,payload) VALUES(?,?,?,?,?,?)",
                (collection, "new-record", "session", "old-session", "active", payload),
            )
        else:
            connection.execute("UPDATE memory_records SET payload=? WHERE collection=?", (payload, collection))


def test_origin_identity_is_idempotent_but_cannot_change_ownership(tmp_path: Path) -> None:
    store = GhostObservationStore(tmp_path)
    assert store.register_input(run_id="same-run", session_id="session")
    assert store.register_input(run_id="same-run", session_id="session")
    with pytest.raises(ValueError, match="another scope"):
        store.register_input(run_id="same-run", session_id="foreign-session")


def test_failed_origin_write_quarantines_the_input_instead_of_learning_without_a_receipt(tmp_path: Path) -> None:
    store = GhostObservationStore(tmp_path)
    with store.database.connection() as connection:
        connection.execute(
            "CREATE TRIGGER reject_origin_fixture BEFORE INSERT ON ghost_input_origins "
            "BEGIN SELECT RAISE(ABORT,'origin write fixture'); END",
        )
    with pytest.raises(sqlite3.IntegrityError, match="origin write fixture"):
        store.register_input(run_id="old-run", session_id="old-session")
    assert not _append(store, "old-run")


def test_checkpoint_not_yet_projected_before_delete_cannot_be_consumed_later(tmp_path: Path) -> None:
    project = str(tmp_path / "project")
    surface = GhostControlSurface.from_state_home(tmp_path)
    _register(surface, project)
    checkpoints = WorkCheckpointStore(tmp_path)
    checkpoint = checkpoints.start(
        run_id="old-run", session_id="old-session", project=Path(project), task="Verify the unfinished input",
    )
    assert checkpoint is not None
    checkpoints.set_status(checkpoint, "interrupted", "fixture incomplete task")
    _delete(surface, "delete_scope", "session", project)
    assert checkpoints.load("old-session") is not None
    reopened = GhostControlSurface.from_state_home(tmp_path)
    assert reopened.work_queue is not None
    result = reopened.work_queue.sync_from_sources(
        work_checkpoint_store=checkpoints, session_id="old-session", project=project,
    )
    assert result.ok
    assert not reopened.work_queue.list_items()


def test_reentering_canonical_task_does_not_refresh_a_deleted_origin(tmp_path: Path) -> None:
    from codey.operations.task_phases.lifecycle import _ensure_durable_task

    request = TaskSubmission("old-session", None, "old input", 2, False, "deepseek", run_id="old-run")
    with closing(AppContext(tmp_path)) as state:
        assert _ensure_durable_task(state, request)
        _delete(GhostControlSurface.from_state_home(tmp_path), "delete_scope", "session", "")
        assert not _ensure_durable_task(state, request)
        assert state.durable.task(request.run_id).conversation_id == request.session_id
        assert state.ghost_observations is not None
        assert not _append(state.ghost_observations, request.run_id)


def _late_process(
    home: Path, ready: Event, release: Event, results: Queue[tuple[bool, bool]],
) -> None:
    surface = GhostControlSurface.from_state_home(home)
    assert surface.observations is not None and surface.continuity is not None
    ready.set()
    if not release.wait(20):
        raise TimeoutError("deletion did not release late process")
    appended = _append(surface.observations, "old-run")
    surface.continuity.sync_from_sources(
        run_id="old-run", session_id="old-session", mode="chat", user_focus_excerpt="old input",
    )
    results.put((appended, bool(surface.continuity.list_items())))


@pytest.mark.parametrize("action", ["delete_scope", "reset_all"])
def test_live_other_process_cannot_materialize_retired_unprojected_input(tmp_path: Path, action: str) -> None:
    surface = GhostControlSurface.from_state_home(tmp_path)
    _register(surface, "")
    context = multiprocessing.get_context("spawn")
    results: Queue[tuple[bool, bool]] = context.Queue()
    ready, release = context.Event(), context.Event()
    process = context.Process(target=_late_process, args=(tmp_path, ready, release, results))
    process.start()
    try:
        assert ready.wait(20)
        _delete(surface, action, "session", "")
        release.set()
        assert results.get(timeout=20) == (False, False)
        process.join(20)
        assert process.exitcode == 0
    finally:
        release.set()
        if process.is_alive():
            process.terminate()
            process.join(5)
        results.close()
        results.join_thread()


@pytest.mark.parametrize("run_id,session_id", [("", "session"), ("run", ""), ("x" * 121, "session"), ("run", "x" * 121)])
def test_origin_identity_cannot_be_truncated_into_an_alias(tmp_path: Path, run_id: str, session_id: str) -> None:
    with pytest.raises(ValueError, match="identity"):
        GhostObservationStore(tmp_path).register_input(run_id=run_id, session_id=session_id)


def test_schema_six_missing_origins_rejects_cold_start(tmp_path: Path) -> None:
    database = GhostSQLite(tmp_path)
    with database.connection() as connection:
        connection.execute("DROP TABLE ghost_input_origins")
    with pytest.raises(GhostSchemaVersionError, match="input origins"):
        GhostSQLite(tmp_path)


def test_schema_five_upgrade_preserves_existing_deletion_fences(tmp_path: Path) -> None:
    store = GhostObservationStore(tmp_path)
    assert _append(store, "old-run")
    store.delete_scope("session", session_id="old-session")
    with store.database.connection() as connection:
        connection.execute("DROP TABLE ghost_input_origins")
        connection.execute("PRAGMA user_version=5")
        before = _integer(connection, "SELECT generation FROM ghost_projection_epoch")
    upgraded = GhostObservationStore(tmp_path)
    assert not upgraded.register_input(run_id="old-run", session_id="old-session")
    assert not _append(upgraded, "old-run")
    with upgraded.database.connection() as connection:
        after = _integer(connection, "SELECT generation FROM ghost_projection_epoch")
        version = _integer(connection, "PRAGMA user_version")
    assert before == after and version == GHOST_SCHEMA_VERSION


def _integer(connection: sqlite3.Connection, statement: str) -> int:
    row: sqlite3.Row | None = connection.execute(statement).fetchone()
    assert row is not None
    value: object = row[0]
    assert type(value) is int
    return value
