from __future__ import annotations

import multiprocessing
from pathlib import Path
from typing import Any

from codey.ghost.deletion_manifest import STORE_COMPLETED, DeletionManifest, new_manifest, store_event
from codey.ghost.deletion_manifest_coordinator import GHOST_STORE_NAMES
from codey.ghost.deletion_manifest_store import DeletionManifestStore
from codey.ghost.sqlite import GhostSQLite


def _manifest(operation_id: str) -> DeletionManifest:
    return new_manifest(
        operation_id=operation_id,
        action="delete_scope",
        scope="project",
        project="P:/multiprocess",
        session_id="session-multiprocess",
        requested_at="2026-10-22T00:00:00Z",
        retention_class="tombstone",
        stores=GHOST_STORE_NAMES,
    )


def _worker(
    state_home: str,
    operation_id: str,
    store_name: str,
    barrier: Any,
    results: Any,
) -> None:
    try:
        barrier.wait(timeout=30)
        database = GhostSQLite(state_home)
        manifests = DeletionManifestStore(database)
        current = manifests.start(_manifest(operation_id))
        manifests.append(store_event(current, store_name, STORE_COMPLETED))
        results.put({"ok": True, "store": store_name})
    except BaseException as exc:  # pragma: no cover - child reports to parent
        results.put({"ok": False, "store": store_name, "error": f"{type(exc).__name__}: {exc}"})


def test_manifest_cold_start_and_events_converge_across_processes(tmp_path: Path) -> None:
    context = multiprocessing.get_context("spawn")
    workers = len(GHOST_STORE_NAMES)
    barrier = context.Barrier(workers)
    results = context.Queue()
    operation_id = "multiprocess-cold-start"
    processes = [
        context.Process(
            target=_worker,
            args=(str(tmp_path), operation_id, store_name, barrier, results),
        )
        for store_name in GHOST_STORE_NAMES
    ]
    for process in processes:
        process.start()
    observed = [results.get(timeout=45) for _ in processes]
    for process in processes:
        process.join(timeout=45)
        assert process.exitcode == 0

    assert all(result["ok"] for result in observed), observed
    database = GhostSQLite(tmp_path)
    manifests = DeletionManifestStore(database)
    manifest = manifests.get(operation_id)
    assert manifest.status == "completed"
    assert manifest.pending_stores == ()
    assert {result["store"] for result in observed} == set(GHOST_STORE_NAMES)
    assert len(manifests.events(operation_id)) == len(GHOST_STORE_NAMES) + 1

