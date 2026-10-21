from __future__ import annotations

import pytest

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
