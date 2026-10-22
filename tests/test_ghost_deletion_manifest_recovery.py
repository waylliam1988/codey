from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from codey.ghost.control_surface import GhostControlSurface
from codey.ghost.deletion_manifest import (
    MANIFEST_BLOCKED,
    MANIFEST_COMPLETED,
    STORE_BLOCKED,
    STORE_COMPLETED,
    STORE_PENDING,
    DeletionManifest,
    new_manifest,
)
from codey.ghost.deletion_manifest_coordinator import (
    GHOST_STORE_NAMES,
    DeletionManifestCoordinator,
    DeletionTarget,
)
from codey.ghost.deletion_manifest_store import DeletionManifestStore
from codey.ghost.schema import GhostSignal
from codey.runs.work_checkpoint import WorkCheckpointStore

PROJECT = "project-delete"
SESSION = "session-delete"


def _seed_all_stores(state_home: Path) -> GhostControlSurface:
    project = state_home / PROJECT
    project.mkdir()
    surface = GhostControlSurface.from_state_home(state_home)
    assert all(
        store is not None
        for store in (
            surface.inbox,
            surface.hebbian,
            surface.continuity,
            surface.sleep,
            surface.work_queue,
            surface.affinity,
            surface.observations,
        )
    )
    assert surface.inbox is not None
    assert surface.hebbian is not None
    assert surface.continuity is not None
    assert surface.sleep is not None
    assert surface.work_queue is not None
    assert surface.affinity is not None
    assert surface.observations is not None

    created = surface.inbox.ingest_signals(
        (
            GhostSignal(
                kind="style_preference",
                scope="project",
                summary="Use the project response structure.",
                evidence_quote="project deletion fixture",
                confidence=0.9,
                metadata={"conflict_key": "delete_fixture", "value_key": "project"},
                source="deletion-recovery-test",
            ),
        ),
        session_id=SESSION,
        run_id="seed-inbox",
        project=str(project),
        user_text="project deletion fixture",
    )
    assert len(created) == 1
    accepted = surface.inbox.review_candidate(created[0].id, "accept", reviewed_by="test")
    assert accepted is not None
    assert surface.hebbian.reinforce_candidate(accepted).applied
    assert surface.continuity.sync_from_sources(
        hebbian_store=surface.hebbian,
        session_id=SESSION,
        run_id="seed-continuity",
        project=str(project),
        mode="chat",
    ).ok

    checkpoints = WorkCheckpointStore(state_home)
    checkpoint = checkpoints.start(
        run_id="seed-work",
        session_id=SESSION,
        project=project,
        task="deletion recovery fixture",
    )
    assert checkpoint is not None
    checkpoints.set_status(checkpoint, "interrupted", "fixture")
    assert surface.work_queue.sync_from_sources(
        continuity_store=surface.continuity,
        work_checkpoint_store=checkpoints,
        session_id=SESSION,
        run_id="seed-work",
        project=str(project),
    ).ok
    assert surface.affinity.sync_from_sources(
        hebbian_store=surface.hebbian,
        work_queue_store=surface.work_queue,
        session_id=SESSION,
        project=str(project),
    ).ok
    surface.sleep.run_once(
        inbox_store=surface.inbox,
        hebbian_store=surface.hebbian,
        continuity_store=surface.continuity,
        work_queue_store=surface.work_queue,
        affinity_store=surface.affinity,
        run_id="seed-sleep",
        session_id=SESSION,
        project=str(project),
    )
    assert surface.observations.append_completed(
        run_id="seed-observation",
        session_id=SESSION,
        mode="chat",
        user_text="deletion recovery fixture",
        assistant_text="seed",
        stop_reason="done",
        provider_id="test",
        project=str(project),
    )
    return surface


def _build_targets(
    surface: GhostControlSurface,
    failing: str,
    calls: list[str],
    injected: list[bool],
) -> dict[str, DeletionTarget]:
    stores: dict[str, Any] = {
        "inbox": surface.inbox,
        "hebbian": surface.hebbian,
        "continuity": surface.continuity,
        "sleep": surface.sleep,
        "work_queue": surface.work_queue,
        "affinity": surface.affinity,
        "observations": surface.observations,
    }
    assert all(store is not None for store in stores.values())
    targets: dict[str, DeletionTarget] = {}
    for name, store in stores.items():
        assert store is not None

        def delete_scope(
            scope: str,
            *,
            project: str = "",
            session_id: str = "",
            name: str = name,
            store: Any = store,
        ) -> object:
            calls.append(name)
            if name == failing and not injected[0]:
                injected[0] = True
                raise OSError(f"injected {name} failure")
            return store.delete_scope(scope, project=project, session_id=session_id)

        if name == "inbox":
            def reset_all(store: Any = store) -> object:
                return store.reset_all(preserve_settings=True)
        else:
            def reset_all(store: Any = store) -> object:
                return store.reset_all()
        targets[name] = DeletionTarget(delete_scope=delete_scope, reset_all=reset_all)
    return targets


def _manifest(operation_id: str, project: str) -> DeletionManifest:
    return new_manifest(
        operation_id=operation_id,
        action="delete_scope",
        scope="project",
        project=project,
        session_id=SESSION,
        requested_at="2026-10-22T00:00:00Z",
        retention_class="tombstone",
        stores=GHOST_STORE_NAMES,
    )


def _assert_no_project_data(surface: GhostControlSurface, project: str) -> None:
    assert surface.inbox is not None
    assert surface.hebbian is not None
    assert surface.continuity is not None
    assert surface.sleep is not None
    assert surface.work_queue is not None
    assert surface.affinity is not None
    assert surface.observations is not None
    assert surface.inbox.list_candidates(scope="project") == ()
    assert all(node.scope_ref != project for node in surface.hebbian.list_nodes(scope="project"))
    assert surface.continuity.list_items(project=project) == ()
    assert surface.work_queue.list_items(project=project) == ()
    sleep = surface.sleep.export_state()
    sleep_payload = sleep.get("sleep")
    report = sleep_payload.get("report") if isinstance(sleep_payload, dict) else None
    assert not isinstance(report, dict) or report.get("project") != project
    affinity = surface.affinity.export_state()["affinity"]
    assert isinstance(affinity, dict)
    assert all(node.get("scope_ref") != project for node in affinity["nodes"])
    observations = surface.observations
    assert observations is not None
    assert observations.read_committed(project=project) == ()


@pytest.mark.parametrize("failing", GHOST_STORE_NAMES)
def test_real_store_failure_restart_and_new_operation_recovery(tmp_path: Path, failing: str) -> None:
    surface = _seed_all_stores(tmp_path)
    calls: list[str] = []
    injected = [False]
    observations = surface.observations
    assert observations is not None
    coordinator = DeletionManifestCoordinator(
        DeletionManifestStore(observations.database),
        _build_targets(surface, failing, calls, injected),
    )
    project = str(tmp_path / PROJECT)
    manifest = _manifest(f"delete-{failing}", project)

    blocked = coordinator.run(manifest)
    assert blocked.status == MANIFEST_BLOCKED
    assert blocked.store(failing).status == STORE_BLOCKED
    assert all(
        blocked.store(name).status == STORE_COMPLETED
        for name in GHOST_STORE_NAMES[: GHOST_STORE_NAMES.index(failing)]
    )
    assert all(
        blocked.store(name).status == STORE_PENDING
        for name in GHOST_STORE_NAMES[GHOST_STORE_NAMES.index(failing) + 1 :]
    )

    first_call_count = len(calls)
    restarted_surface = GhostControlSurface.from_state_home(tmp_path)
    restarted_observations = restarted_surface.observations
    assert restarted_observations is not None
    resumed = DeletionManifestCoordinator(
        DeletionManifestStore(restarted_observations.database),
        _build_targets(restarted_surface, failing, calls, injected),
    ).run(manifest)
    assert resumed.status == MANIFEST_BLOCKED
    assert resumed.store(failing).status == STORE_BLOCKED
    assert calls.count(failing) == 1
    resumed_calls = calls[first_call_count:]
    expected_pending = GHOST_STORE_NAMES[GHOST_STORE_NAMES.index(failing) + 1 :]
    assert tuple(resumed_calls) == expected_pending

    # A blocked store is terminal for this operation. A new operation is the
    # explicit recovery boundary once the external fault is gone.
    recovered_surface = GhostControlSurface.from_state_home(tmp_path)
    recovered_observations = recovered_surface.observations
    assert recovered_observations is not None
    retry_calls: list[str] = []
    retry = DeletionManifestCoordinator(
        DeletionManifestStore(recovered_observations.database),
        _build_targets(recovered_surface, "", retry_calls, [False]),
    ).run(_manifest(f"retry-{failing}", project))
    assert retry.status == MANIFEST_COMPLETED
    assert all(retry.store(name).status == STORE_COMPLETED for name in GHOST_STORE_NAMES)

    rebuilt = GhostControlSurface.from_state_home(tmp_path)
    _assert_no_project_data(rebuilt, project)
    assert rebuilt.hebbian is not None
    assert rebuilt.continuity is not None
    assert rebuilt.work_queue is not None
    assert rebuilt.affinity is not None
    assert rebuilt.hebbian.rebuild_from_events()
    assert rebuilt.continuity.rebuild_from_events()
    assert rebuilt.work_queue.rebuild_from_events()
    assert rebuilt.affinity.rebuild_from_events()
    _assert_no_project_data(rebuilt, project)
