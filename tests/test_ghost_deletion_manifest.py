from __future__ import annotations

import pytest

from codey.ghost.control_surface import GhostControlSurface
from codey.ghost.deletion_manifest import (
    MANIFEST_BLOCKED,
    MANIFEST_COMPLETED,
    STORE_BLOCKED,
    STORE_COMPLETED,
    DeletionManifestError,
    apply_store_event,
    new_manifest,
    replay,
    started_event,
    store_event,
)
from codey.ghost.deletion_manifest_coordinator import (
    GHOST_STORE_NAMES,
    DeletionManifestCoordinator,
    DeletionTarget,
)
from codey.ghost.deletion_manifest_store import DeletionManifestStore
from codey.ghost.sqlite import GhostSQLite


def _manifest():
    return new_manifest(
        operation_id="delete-1",
        action="delete_scope",
        scope="project",
        project="P:/demo",
        requested_at="2026-10-22T00:00:00Z",
        retention_class="tombstone",
        stores=("inbox", "continuity", "work_queue"),
    )


def test_manifest_digest_is_deterministic_and_excludes_operation_id() -> None:
    first = _manifest()
    second = new_manifest(
        operation_id="delete-2",
        action="delete_scope",
        scope="project",
        project="P:/demo",
        requested_at="later",
        retention_class="tombstone",
        stores=("inbox", "continuity", "work_queue"),
    )
    assert first.tombstone_digest == second.tombstone_digest
    assert first.pending_stores == ("inbox", "continuity", "work_queue")


def test_replay_is_idempotent_and_completes_only_after_all_stores() -> None:
    manifest = _manifest()
    events = [started_event(manifest)]
    for store in manifest.pending_stores:
        events.append(store_event(manifest, store, STORE_COMPLETED))
        manifest = apply_store_event(manifest, events[-1])
    replayed = replay(events)
    assert replayed.status == MANIFEST_COMPLETED
    assert replayed.pending_stores == ()
    assert replayed.to_payload() == manifest.to_payload()


def test_blocked_store_is_terminal_and_replayable() -> None:
    manifest = _manifest()
    events = [started_event(manifest), store_event(manifest, "continuity", STORE_BLOCKED, detail="disk full")]
    blocked = replay(events)
    assert blocked.status == MANIFEST_BLOCKED
    assert blocked.store("continuity").detail == "disk full"
    progressed = apply_store_event(blocked, store_event(blocked, "inbox", STORE_COMPLETED))
    assert progressed.status == MANIFEST_BLOCKED
    with pytest.raises(DeletionManifestError, match="conflicting transition"):
        apply_store_event(blocked, store_event(blocked, "continuity", STORE_COMPLETED))


def test_corrupt_or_out_of_order_events_fail_closed() -> None:
    manifest = _manifest()
    with pytest.raises(DeletionManifestError, match="must start"):
        replay([store_event(manifest, "inbox", STORE_COMPLETED)])
    with pytest.raises(DeletionManifestError, match="unsupported manifest schema"):
        replay([{"schema_version": 99, "kind": "started", "event_id": "x", "operation_id": "delete-1", "manifest": {}}])


def test_completed_manifest_cannot_contain_pending_store() -> None:
    manifest = _manifest()
    with pytest.raises(DeletionManifestError, match="unfinished"):
        type(manifest)(**{**manifest.__dict__, "status": MANIFEST_COMPLETED})


def test_manifest_store_survives_restart_and_idempotent_retry(tmp_path) -> None:
    manifest = _manifest()
    first = DeletionManifestStore(GhostSQLite(tmp_path))
    assert first.start(manifest).to_payload() == manifest.to_payload()
    event = store_event(manifest, "inbox", STORE_COMPLETED)
    assert first.append(event).store("inbox").status == STORE_COMPLETED
    assert first.append(event).store("inbox").status == STORE_COMPLETED

    restarted = DeletionManifestStore(GhostSQLite(tmp_path))
    current = restarted.get(manifest.operation_id)
    assert current.store("inbox").status == STORE_COMPLETED
    assert len(restarted.events(manifest.operation_id)) == 2


def test_manifest_store_rejects_operation_reuse_and_corrupts_closed_state(tmp_path) -> None:
    store = DeletionManifestStore(GhostSQLite(tmp_path))
    manifest = _manifest()
    store.start(manifest)
    different = new_manifest(
        operation_id=manifest.operation_id, action="reset_all", scope="all",
        requested_at=manifest.requested_at, retention_class="purge", stores=("inbox",),
    )
    with pytest.raises(DeletionManifestError, match="different manifest"):
        store.start(different)
    with GhostSQLite(tmp_path).connection() as database:
        database.execute(
            "UPDATE ghost_deletion_manifests SET manifest=? WHERE operation_id=?",
            ('{"schema_version":1}', manifest.operation_id),
        )
    with pytest.raises(DeletionManifestError, match="manifest stores"):
        store.get(manifest.operation_id)


def test_coordinator_retries_only_pending_stores_after_a_blocked_store(tmp_path) -> None:
    calls: list[str] = []
    fail = {"continuity"}

    def target(name: str) -> DeletionTarget:
        def delete_scope(_scope: str, **_kwargs: object) -> dict[str, str]:
            calls.append(name)
            if name in fail:
                fail.remove(name)
                raise OSError("temporary store fault")
            return {"store": name}

        def reset_all() -> bool:
            calls.append(name)
            return True

        return DeletionTarget(delete_scope=delete_scope, reset_all=reset_all)

    database = GhostSQLite(tmp_path)
    store = DeletionManifestStore(database)
    coordinator = DeletionManifestCoordinator(store, {name: target(name) for name in GHOST_STORE_NAMES})
    manifest = new_manifest(
        operation_id="delete-retry", action="delete_scope", scope="project",
        project="P:/demo", requested_at="2026-10-22T00:00:00Z", retention_class="tombstone",
        stores=GHOST_STORE_NAMES,
    )
    blocked = coordinator.run(manifest, project="P:/demo")
    assert blocked.status == MANIFEST_BLOCKED
    assert calls == ["inbox", "hebbian", "continuity"]

    resumed = coordinator.run(manifest, project="P:/demo")
    assert resumed.status == MANIFEST_BLOCKED
    assert calls == ["inbox", "hebbian", "continuity", "sleep", "work_queue", "affinity", "observations"]
    assert resumed.store("inbox").status == STORE_COMPLETED
    assert resumed.store("continuity").status == STORE_BLOCKED
    assert resumed.pending_stores == ()


def test_coordinator_requires_exactly_the_existing_seven_store_targets(tmp_path) -> None:
    store = DeletionManifestStore(GhostSQLite(tmp_path))
    target = DeletionTarget(delete_scope=lambda *_args, **_kwargs: None, reset_all=lambda: None)
    with pytest.raises(ValueError, match="exactly the seven"):
        DeletionManifestCoordinator(store, {name: target for name in GHOST_STORE_NAMES[:-1]})


def test_coordinator_binds_the_existing_seven_store_reset_methods(tmp_path) -> None:
    surface = GhostControlSurface.from_state_home(tmp_path)
    stores = {
        "inbox": surface.inbox,
        "hebbian": surface.hebbian,
        "continuity": surface.continuity,
        "sleep": surface.sleep,
        "work_queue": surface.work_queue,
        "affinity": surface.affinity,
        "observations": surface.observations,
    }
    targets = {}
    for name, ghost_store in stores.items():
        assert ghost_store is not None
        reset = (lambda store=ghost_store: store.reset_all(preserve_settings=True)) if name == "inbox" else ghost_store.reset_all
        targets[name] = DeletionTarget(delete_scope=ghost_store.delete_scope, reset_all=reset)
    manifest = new_manifest(
        operation_id="reset-seven-stores", action="reset_all", scope="all",
        requested_at="2026-10-22T00:00:00Z", retention_class="tombstone", stores=GHOST_STORE_NAMES,
    )
    result = DeletionManifestCoordinator(
        DeletionManifestStore(GhostSQLite(tmp_path)), targets,
    ).run(manifest)
    assert result.status == MANIFEST_COMPLETED
    assert result.pending_stores == ()
    restarted = GhostControlSurface.from_state_home(tmp_path)
    assert restarted.export_state()["ok"] is True
