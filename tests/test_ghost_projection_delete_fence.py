"""Derived projections cannot commit inputs read before a deletion boundary."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from codey.ghost.control_surface import GhostControlSurface
from codey.ghost.sqlite import GhostProjectionInvalidatedError, GhostSchemaVersionError, GhostSQLite
from tests.manual.ghost_memory_routing.deletion_runtime_probe import _seed
from tests.manual.ghost_memory_routing.projection_delete_probe import cell, has_projection, seed_derived


@pytest.mark.parametrize("action", ["delete_scope", "reset_all"])
@pytest.mark.parametrize("writer", ["continuity", "work_queue", "affinity", "sleep"])
@pytest.mark.parametrize("order", ["writer_first", "delete_first"])
def test_real_projection_writer_cannot_restore_deleted_inputs(
    tmp_path: Path, action: str, writer: str, order: str,
) -> None:
    result = cell(tmp_path, action, writer, order, 0)
    assert result.delete_ok
    assert result.rebuild_ok
    assert not result.resurrected
    assert result.writer_ok == (order == "writer_first")


def test_fresh_sources_after_deletion_can_rebuild_all_derived_consumers(tmp_path: Path) -> None:
    surface = GhostControlSurface.from_state_home(tmp_path)
    project = str(tmp_path / "project")
    seed_derived(surface, project)
    status, _ = surface.dispatch_action({"action": "reset_all", "confirm": True})
    assert status == 200
    fresh = GhostControlSurface.from_state_home(tmp_path)
    assert fresh.continuity is not None
    assert fresh.work_queue is not None
    assert fresh.affinity is not None
    assert fresh.sleep is not None
    _seed(fresh, project, run_id="new-run")
    assert fresh.continuity.sync_from_sources(
        hebbian_store=fresh.hebbian, user_focus_excerpt="What should we check next?",
        run_id="new-run", project=project, mode="chat",
    ).ok
    assert fresh.work_queue.sync_from_sources(continuity_store=fresh.continuity, project=project).ok
    assert fresh.affinity.sync_from_sources(
        hebbian_store=fresh.hebbian, work_queue_store=fresh.work_queue, project=project,
    ).ok
    assert fresh.sleep.run_once(run_id="new-run", project=project).steps[-1].ok
    projections = {
        writer: has_projection(fresh, writer, project=project)
        for writer in ("continuity", "work_queue", "affinity", "sleep")
    }
    assert all(projections.values()), projections


def test_noop_delete_invalidates_snapshot_and_nested_context_keeps_original_generation(tmp_path: Path) -> None:
    database = GhostSQLite(tmp_path)
    surface = GhostControlSurface.from_state_home(tmp_path)
    assert surface.continuity is not None

    def delete() -> int:
        assert surface.continuity is not None
        return surface.continuity.delete_scope("project", project=str(tmp_path / "empty"))

    with database.projection_inputs():
        with ThreadPoolExecutor(max_workers=1) as executor:
            assert executor.submit(delete).result(timeout=10) == 0
        with (
            database.projection_inputs(),
            pytest.raises(GhostProjectionInvalidatedError, match="invalidated"),
            surface.continuity.records.transaction(),
        ):
            raise AssertionError("stale projection must not enter the write body")
    # Leaving the logical operation releases its snapshot; a new operation is valid.
    assert surface.continuity.sync_from_sources(user_focus_excerpt="fresh", mode="chat").ok


def test_deletion_in_another_state_home_does_not_invalidate_projection(tmp_path: Path) -> None:
    first = GhostControlSurface.from_state_home(tmp_path / "first")
    second = GhostControlSurface.from_state_home(tmp_path / "second")
    assert first.continuity is not None
    assert second.continuity is not None
    with first.continuity.database.projection_inputs():
        second.continuity.reset_all()
        assert first.continuity.sync_from_sources(user_focus_excerpt="kept", mode="chat").ok
    assert has_projection(first, "continuity")


@pytest.mark.parametrize("damage", ["missing_table", "missing_row"])
def test_lost_deletion_generation_never_silently_resets_snapshot(tmp_path: Path, damage: str) -> None:
    database = GhostSQLite(tmp_path)
    with database.connection() as connection:
        connection.execute(
            "DROP TABLE ghost_projection_epoch" if damage == "missing_table"
            else "DELETE FROM ghost_projection_epoch",
        )
    with pytest.raises(GhostProjectionInvalidatedError), database.projection_inputs():
        raise AssertionError("missing deletion generation must not accept inputs")


def test_long_project_deletion_replay_preserves_exact_scope(tmp_path: Path) -> None:
    surface = GhostControlSurface.from_state_home(tmp_path)
    assert surface.continuity is not None
    project = str(tmp_path / ("x" * (220 - len(str(tmp_path)) - 1)))
    assert len(project) > 200
    assert len(project) <= 240
    assert surface.continuity.sync_from_sources(
        user_focus_excerpt="What remains?", mode="chat", project=project, run_id="long-run",
    ).ok
    assert surface.continuity.delete_scope("project", project=project) == 2
    assert surface.continuity.rebuild_from_events()
    assert surface.continuity.list_items() == ()


def test_missing_projection_epoch_table_rejects_cold_start(tmp_path: Path) -> None:
    database = GhostSQLite(tmp_path)
    with database.connection() as connection:
        connection.execute("DROP TABLE ghost_projection_epoch")
    with pytest.raises(GhostSchemaVersionError, match="generation is missing"):
        GhostSQLite(tmp_path)
