"""A refused lock acquisition is not a committed, subsequently lost write."""

from concurrent.futures import ThreadPoolExecutor
from threading import Event
from unittest.mock import patch

import pytest

from codey.storage import file_lock
from codey.workspace import changes
from codey.workspace.changes import SnapshotStore


def test_contended_write_is_explicitly_rejected_and_retry_preserves_first_baseline(tmp_path):
    root = tmp_path / "project"
    root.mkdir()
    store = SnapshotStore(tmp_path / "state")
    entered = Event()
    release = Event()
    write_body = changes._write_bytes_atomic

    def paused_body_write(path, data):
        entered.set()
        assert release.wait(timeout=15), "test failed to release the baseline writer"
        write_body(path, data)

    with (
        ThreadPoolExecutor(max_workers=2) as pool,
        patch.object(changes, "_write_bytes_atomic", side_effect=paused_body_write),
    ):
        holder = pool.submit(store.put_baseline, root, "first.txt", "original")
        try:
            assert entered.wait(timeout=10), "baseline writer did not enter its transaction"
            # Events establish contention; no sleep or disk-speed assumption.
            with patch.object(file_lock, "LOCK_TIMEOUT_SECONDS", 0):
                rejected = pool.submit(store.put_baseline, root, "second.txt", "second")
                with pytest.raises(file_lock.LockTimeout, match="thread lock"):
                    rejected.result(timeout=10)
            assert not store._baseline_path(root, "second.txt").exists()
        finally:
            release.set()
        assert holder.result(timeout=10) == "original"

    assert store.load(root) == ({"first.txt": "original"}, {})
    assert store.put_baseline(root, "first.txt", "newer") == "original"
    assert store.put_baseline(root, "second.txt", "second") == "second"
    assert store.load(root) == ({"first.txt": "original", "second.txt": "second"}, {})
