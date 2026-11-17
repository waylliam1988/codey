"""Fresh source reads must not relearn explicitly deleted Ghost projections."""

from __future__ import annotations

import multiprocessing
import sqlite3
from multiprocessing.queues import Queue
from multiprocessing.synchronize import Event
from pathlib import Path
from unittest.mock import patch

import pytest

from codey.ghost.control_surface import GhostControlSurface
from codey.ghost.sqlite import GhostSchemaVersionError, GhostSQLite
from codey.runs.work_checkpoint import WorkCheckpointStore
from tests.manual.ghost_memory_routing.deletion_runtime_probe import _seed


def _sync(
    surface: GhostControlSurface, consumer: str, home: Path, *, run_id: str = "source-run",
    task: str = "What still needs verification?", scope: str = "session",
) -> tuple[str, ...]:
    project = str(home / "project") if scope != "user" else ""
    session_id = "source-session" if scope == "session" else ""
    if consumer == "continuity":
        assert surface.continuity is not None
        result = surface.continuity.sync_from_sources(
            user_focus_excerpt=task, run_id=run_id, project=project, session_id=session_id, mode="chat",
        )
        assert result.ok, result
        return tuple(item.source_ref for item in surface.continuity.list_items())
    checkpoints = WorkCheckpointStore(home)
    checkpoint = checkpoints.start(
        run_id=run_id, session_id=session_id, project=Path(project), task=task,
    )
    assert checkpoint is not None
    checkpoints.set_status(checkpoint, "interrupted", "source lifetime fixture")
    assert surface.work_queue is not None
    result_work = surface.work_queue.sync_from_sources(
        work_checkpoint_store=checkpoints, session_id=session_id, project=project,
    )
    assert result_work.ok, result_work
    return tuple(item.source_ref for item in surface.work_queue.list_items())


@pytest.mark.parametrize("consumer", ["continuity", "work_checkpoint"])
@pytest.mark.parametrize("action", ["delete_scope", "reset_all"])
@pytest.mark.parametrize("changed_text", [False, True])
def test_source_read_after_delete_does_not_recreate_old_projection(
    tmp_path: Path, consumer: str, action: str, changed_text: bool,
) -> None:
    surface = GhostControlSurface.from_state_home(tmp_path)
    assert _sync(surface, consumer, tmp_path)
    status, payload = surface.dispatch_action({
        "action": action, "scope": "session", "session_id": "source-session", "confirm": True,
    })
    assert status == 200, payload
    if consumer == "work_checkpoint":
        # Canonical task state belongs to runtime and must survive Ghost deletion.
        assert WorkCheckpointStore(tmp_path).load("source-session") is not None
    reopened = GhostControlSurface.from_state_home(tmp_path)
    assert reopened.continuity is not None
    assert reopened.work_queue is not None
    assert reopened.continuity.rebuild_from_events()
    assert reopened.work_queue.rebuild_from_events()
    assert reopened.continuity.list_items() == ()
    assert reopened.work_queue.list_items() == ()
    task = "What changed in the same source?" if changed_text else "What still needs verification?"
    assert _sync(reopened, consumer, tmp_path, task=task) == ()
    fresh_refs = _sync(reopened, consumer, tmp_path, run_id="fresh-source-run", task=task)
    assert fresh_refs
    assert set(fresh_refs) == {"fresh-source-run"}


@pytest.mark.parametrize("scope", ["user", "project"])
@pytest.mark.parametrize("action", ["delete_scope", "reset_all"])
def test_continuity_source_retirement_covers_other_scopes(
    tmp_path: Path, scope: str, action: str,
) -> None:
    surface = GhostControlSurface.from_state_home(tmp_path)
    assert _sync(surface, "continuity", tmp_path, scope=scope)
    status, payload = surface.dispatch_action({
        "action": action, "scope": scope, "project": str(tmp_path / "project"), "confirm": True,
    })
    assert status == 200, payload
    assert _sync(GhostControlSurface.from_state_home(tmp_path), "continuity", tmp_path, scope=scope) == ()


def test_retiring_one_scope_preserves_same_source_in_another_scope(tmp_path: Path) -> None:
    surface = GhostControlSurface.from_state_home(tmp_path)
    assert _sync(surface, "continuity", tmp_path, scope="project")
    assert _sync(surface, "continuity", tmp_path, scope="session")
    status, _ = surface.dispatch_action({
        "action": "delete_scope", "scope": "project", "project": str(tmp_path / "project"), "confirm": True,
    })
    assert status == 200
    reopened = GhostControlSurface.from_state_home(tmp_path)
    assert _sync(reopened, "continuity", tmp_path, scope="session")
    assert reopened.continuity is not None
    assert {item.scope for item in reopened.continuity.list_items()} == {"session"}


@pytest.mark.parametrize("consumer", ["continuity", "work_checkpoint"])
def test_source_retirement_survives_event_compaction_and_another_reset(tmp_path: Path, consumer: str) -> None:
    surface = GhostControlSurface.from_state_home(tmp_path)
    assert _sync(surface, consumer, tmp_path)
    module = "continuity" if consumer == "continuity" else "work_queue"
    constant = "MAX_CONTINUITY_EVENTS" if consumer == "continuity" else "MAX_WORK_EVENTS"
    with patch(f"codey.ghost.{module}.{constant}", 0):
        status, _ = surface.dispatch_action({
            "action": "delete_scope", "scope": "session", "session_id": "source-session", "confirm": True,
        })
        assert status == 200
        assert _sync(surface, consumer, tmp_path, run_id="other-run")
    status, _ = surface.dispatch_action({"action": "reset_all", "confirm": True})
    assert status == 200
    assert _sync(GhostControlSurface.from_state_home(tmp_path), consumer, tmp_path) == ()


@pytest.mark.parametrize("mutation", ["insert", "update"])
def test_storage_write_cannot_reintroduce_retired_source_under_new_record_id(tmp_path: Path, mutation: str) -> None:
    surface = GhostControlSurface.from_state_home(tmp_path)
    assert _sync(surface, "continuity", tmp_path)
    assert surface.continuity is not None
    old_rows = surface.continuity.records.rows()
    status, _ = surface.dispatch_action({"action": "reset_all", "confirm": True})
    assert status == 200
    assert _sync(surface, "continuity", tmp_path, run_id="other-run")
    if mutation == "insert":
        row = dict(old_rows[0])
        row["id"] = "new-id-cannot-bypass-retirement"
        with pytest.raises(sqlite3.IntegrityError, match="deleted Ghost source"):
            surface.continuity.records.replace_rows((row,))
    else:
        with (
            pytest.raises(sqlite3.IntegrityError, match="deleted Ghost source"),
            surface.continuity.database.connection() as connection,
        ):
            connection.execute(
                "UPDATE memory_records SET payload=json_set(payload,'$.source_ref','source-run') "
                "WHERE collection='continuity'",
            )
    assert {item.source_ref for item in surface.continuity.list_items()} == {"other-run"}


def test_missing_source_retirement_is_not_silently_recreated(tmp_path: Path) -> None:
    database = GhostSQLite(tmp_path)
    with database.connection() as connection:
        connection.execute("DROP TABLE ghost_retired_sources")
    with pytest.raises(GhostSchemaVersionError, match="source retirement is missing"):
        GhostSQLite(tmp_path)


def test_v4_upgrade_preserves_prior_generation_and_can_retire_new_deletions(tmp_path: Path) -> None:
    database = GhostSQLite(tmp_path)
    with database.connection() as connection:
        connection.execute("DROP TRIGGER ghost_reject_retired_source_insert")
        connection.execute("DROP TRIGGER ghost_reject_retired_source_update")
        connection.execute("DROP TABLE ghost_retired_sources")
        connection.execute("UPDATE ghost_projection_epoch SET generation=17")
        connection.execute("DROP TABLE ghost_replay_windows")
        connection.execute("PRAGMA user_version=4")
    surface = GhostControlSurface.from_state_home(tmp_path)
    with database.connection() as connection:
        row: sqlite3.Row | None = connection.execute("SELECT generation FROM ghost_projection_epoch").fetchone()
        assert row is not None
        generation: object = row[0]
        assert generation == 17
    assert _sync(surface, "continuity", tmp_path)
    status, _ = surface.dispatch_action({"action": "reset_all", "confirm": True})
    assert status == 200
    assert _sync(surface, "continuity", tmp_path) == ()


def _source_worker(
    home: Path, consumer: str, ready: Event, release: Event, results: Queue[tuple[str, ...]],
) -> None:
    surface = GhostControlSurface.from_state_home(home)
    ready.set()
    if not release.wait(20):
        raise TimeoutError("deletion did not release source consumer")
    results.put(_sync(surface, consumer, home))


@pytest.mark.parametrize("consumer", ["continuity", "work_checkpoint"])
@pytest.mark.parametrize("action", ["delete_scope", "reset_all"])
def test_another_live_process_respects_committed_source_retirement(
    tmp_path: Path, consumer: str, action: str,
) -> None:
    surface = GhostControlSurface.from_state_home(tmp_path)
    assert _sync(surface, consumer, tmp_path)
    context = multiprocessing.get_context("spawn")
    results: Queue[tuple[str, ...]] = context.Queue()
    ready, release = context.Event(), context.Event()
    process = context.Process(target=_source_worker, args=(tmp_path, consumer, ready, release, results))
    process.start()
    try:
        assert ready.wait(20), "source worker did not open its independent owner"
        status, payload = surface.dispatch_action({
            "action": action, "scope": "session", "session_id": "source-session", "confirm": True,
        })
        assert status == 200, payload
        release.set()
        process.join(20)
        assert process.exitcode == 0
        assert results.get(timeout=2) == ()
    finally:
        release.set()
        if process.is_alive():
            process.terminate()
            process.join(5)
        results.close()
        results.join_thread()


@pytest.mark.parametrize("action", ["delete_scope", "reset_all"])
def test_new_manual_confirmation_can_reuse_preference_slot_after_deletion(tmp_path: Path, action: str) -> None:
    surface = GhostControlSurface.from_state_home(tmp_path)
    project = str(tmp_path / "project")
    old_candidate = _seed(surface, project)
    assert surface.continuity is not None
    assert surface.continuity.sync_from_sources(hebbian_store=surface.hebbian).ok
    assert surface.continuity.list_items()
    status, _ = surface.dispatch_action({
        "action": action, "scope": "project", "project": project, "confirm": True,
    })
    assert status == 200
    fresh_candidate = _seed(surface, project, run_id="fresh-source-run")
    assert fresh_candidate.id != old_candidate.id
    assert surface.continuity.sync_from_sources(hebbian_store=surface.hebbian).ok
    assert surface.continuity.list_items(), "new accepted evidence must not be mistaken for the old source"
