"""Project writer ownership uses real OS leases and retains failed cleanup."""

import threading
from pathlib import Path

import pytest

from codey.app.workspace_leases import WorkspaceLeaseCoordinator
from codey.workspace.changes import SnapshotStore


def test_project_leases_are_exclusive_and_reusable_after_release(tmp_path: Path) -> None:
    root = tmp_path / "project"
    root.mkdir()
    store = SnapshotStore(tmp_path / "state")
    first = WorkspaceLeaseCoordinator(threading.Lock(), lambda: store)
    second = WorkspaceLeaseCoordinator(threading.Lock(), lambda: store)
    try:
        assert first.acquire("") and not first.leases
        assert first.acquire(root)
        assert not first.acquire(root / ".")
        assert not second.acquire(root)
        first.release(root)
        first.release(root)
        assert not first.leases
        assert second.acquire(root)
    finally:
        assert first.close()
        assert second.close()


def test_failed_project_cleanup_retains_ownership_for_retry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "project"
    root.mkdir()
    store = SnapshotStore(tmp_path / "state")
    owner = WorkspaceLeaseCoordinator(threading.Lock(), lambda: store)
    contender = WorkspaceLeaseCoordinator(threading.Lock(), lambda: store)
    assert owner.acquire(root)
    key = str(root.resolve())
    lease = owner.leases[key]
    release = lease.release
    attempts = 0

    def busy_once() -> None:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise OSError("project cleanup is busy")
        release()

    monkeypatch.setattr(lease, "release", busy_once)
    try:
        assert not owner.close()
        assert owner.leases[key] is lease
        assert not contender.acquire(root)
        assert owner.close()
        assert not owner.leases and attempts == 2
        assert contender.acquire(root)
    finally:
        assert owner.close()
        assert contender.close()
