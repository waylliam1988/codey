"""Deletion fences stale inputs without disabling new memories in the scope."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from codey.ghost.control_surface import GhostControlSurface
from codey.ghost.deletion_manifest import new_manifest
from codey.ghost.deletion_manifest_coordinator import (
    GHOST_STORE_NAMES,
    DeletionManifestCoordinator,
    DeletionTarget,
)
from codey.ghost.deletion_manifest_store import DeletionManifestStore
from codey.ghost.observations import GhostObservationStore
from codey.ghost.sqlite import GHOST_SCHEMA_VERSION, GhostSchemaVersionError, GhostSQLite
from tests.manual.ghost_memory_routing.deletion_runtime_probe import _cell, _seed, _targets


@pytest.mark.parametrize("action", ["delete_scope", "reset_all"])
@pytest.mark.parametrize("writer", ["hebbian", "observations"])
@pytest.mark.parametrize("order", ["writer_first", "delete_first"])
def test_real_writer_delete_interleaving_cannot_revive_input(
    tmp_path: Path, action: str, writer: str, order: str,
) -> None:
    cell = _cell(tmp_path, action, writer, order, 0)
    assert cell.manifest_status == "completed"
    assert cell.rebuild_ok
    assert not cell.resurrected
    if order == "delete_first":
        assert not cell.writer_applied


@pytest.mark.parametrize("scope", ["user", "project", "session", "all"])
def test_default_control_delete_fences_old_candidate_but_accepts_new_identity(
    tmp_path: Path, scope: str,
) -> None:
    surface = GhostControlSurface.from_state_home(tmp_path)
    project = str(tmp_path / "project")
    candidate = _seed(surface, project, scope="project" if scope == "all" else scope)
    action = "reset_all" if scope == "all" else "delete_scope"
    status, response = surface.dispatch_action({
        "action": action, "confirm": True, "scope": scope, "project": project, "session_id": "session",
    })
    assert status == 200, response
    restarted = GhostControlSurface.from_state_home(tmp_path)
    assert restarted.hebbian is not None
    assert restarted.inbox is not None
    assert not restarted.hebbian.reinforce_candidate(candidate).applied
    assert restarted.hebbian.list_nodes() == ()
    # Relearning the same slot/value has a new source identity and remains usable.
    new_candidate = _seed(restarted, project, run_id="new-run", scope="project" if scope == "all" else scope)
    assert candidate.id != new_candidate.id
    assert len(restarted.hebbian.list_nodes()) == 1
    assert restarted.hebbian.rebuild_from_events()
    assert restarted.inbox.compact_if_needed()
    assert len(restarted.hebbian.list_nodes()) == 1
    assert not restarted.hebbian.reinforce_candidate(candidate).applied


def test_other_scope_survives_and_idempotent_existing_observation_remains_writable(tmp_path: Path) -> None:
    surface = GhostControlSurface.from_state_home(tmp_path)
    old = _seed(surface, str(tmp_path / "old"))
    kept = _seed(surface, str(tmp_path / "kept"), run_id="kept-run")
    status, _ = surface.dispatch_action({
        "action": "delete_scope", "scope": "project", "project": str(tmp_path / "old"), "confirm": True,
    })
    assert status == 200
    assert surface.hebbian is not None
    assert {ref for node in surface.hebbian.list_nodes() for ref in node.candidate_ids} == {kept.id}
    assert not surface.hebbian.reinforce_candidate(old).applied
    observations = GhostObservationStore(tmp_path)
    assert observations.append_completed(
        run_id="kept-run", session_id="session", project=str(tmp_path / "kept"), mode="chat",
        user_text="kept", assistant_text="updated", stop_reason="done",
    )
    assert len(observations.read_all()) == 1


@pytest.mark.parametrize("action", ["delete_scope", "reset_all"])
@pytest.mark.parametrize("result", [False, 0, None])
def test_coordinator_does_not_mark_explicit_failure_as_completed(
    tmp_path: Path, action: str, result: bool | int | None,
) -> None:
    surface = GhostControlSurface.from_state_home(tmp_path)
    assert surface.observations is not None
    targets = _targets(surface)

    def delete_scope(scope: str, *, project: str = "", session_id: str = "") -> object:
        return result

    def reset_all() -> object:
        return result

    targets["inbox"] = DeletionTarget(delete_scope, reset_all)
    manifests = DeletionManifestStore(surface.observations.database)
    manifest = new_manifest(
        operation_id="explicit-failure", action=action, scope="all" if action == "reset_all" else "project",
        project="project", requested_at="2026-11-17T00:00:00Z", retention_class="tombstone",
        stores=GHOST_STORE_NAMES,
    )
    completed = DeletionManifestCoordinator(manifests, targets).run(manifest)
    assert completed.status == ("blocked" if result is False else "completed")
    assert manifests.get(manifest.operation_id) == completed


def test_v2_upgrade_installs_fences_and_reset_never_forgets_retired_run_ids(tmp_path: Path) -> None:
    database = GhostSQLite(tmp_path)
    with database.connection() as connection:
        connection.execute("DROP TRIGGER ghost_reject_retired_observation")
        connection.execute("DROP TRIGGER ghost_reject_retired_graph_input")
        connection.execute("DROP TRIGGER ghost_reject_retired_candidate")
        connection.execute("DROP TABLE ghost_retired_inputs")
        connection.execute("PRAGMA user_version=2")
    surface = GhostControlSurface.from_state_home(tmp_path)
    _seed(surface, str(tmp_path / "project"))
    assert surface.observations is not None
    assert surface.observations.delete_scope("user") == 1
    surface.observations.reset_all()
    reopened = GhostObservationStore(tmp_path)
    assert not reopened.append_completed(
        run_id="old-run", session_id="other", project="different", mode="chat",
        user_text="stale", assistant_text="stale", stop_reason="done",
    )
    with sqlite3.connect(database.path) as connection:
        assert connection.execute("PRAGMA user_version").fetchone() == (GHOST_SCHEMA_VERSION,)
        assert connection.execute("SELECT count(*) FROM ghost_retired_inputs").fetchone() == (1,)


def test_existing_schema_does_not_silently_recreate_lost_deletion_fences(tmp_path: Path) -> None:
    database = GhostSQLite(tmp_path)
    with database.connection() as connection:
        connection.execute("DROP TABLE ghost_retired_inputs")
    with pytest.raises(GhostSchemaVersionError, match="deletion fences are missing"):
        GhostSQLite(tmp_path)
