"""Publication can precede a durability error; referenced bodies must survive."""

import errno
from unittest.mock import patch

import pytest

from codey.storage import atomic_io
from codey.storage.local_store import StoreCorruption
from codey.workspace import changes
from codey.workspace.changes import ChangeTracker, SnapshotStore


@pytest.mark.parametrize("unreadable_after_error", [False, True])
def test_published_manifest_error_preserves_baseline_and_retry_adopts_it(tmp_path, unreadable_after_error):
    root = tmp_path / "project"
    root.mkdir()
    source = root / "app.py"
    source.write_text("original\n", encoding="utf-8")
    store = SnapshotStore(tmp_path / "state")
    tracker = ChangeTracker(root, store)
    read_manifest = changes.read_json_strict
    published = False

    def fail_after_manifest_replace(directory):
        nonlocal published
        if directory == store.path_for(root).parent:
            published = True
            raise OSError(errno.EIO, "manifest directory fsync failed")

    def read_or_fail(path, **kwargs):
        if published and unreadable_after_error:
            raise StoreCorruption(path, "unreadable after publication")
        return read_manifest(path, **kwargs)

    with (
        patch.object(atomic_io, "_fsync_dir", side_effect=fail_after_manifest_replace),
        patch.object(changes, "read_json_strict", side_effect=read_or_fail),
        pytest.raises(OSError, match="manifest directory fsync failed"),
    ):
        tracker.capture_before("app.py")

    assert published
    assert not tracker.has_snapshots  # Failure is still surfaced to the caller.
    assert store._baseline_path(root, "app.py").read_text(encoding="utf-8") == "original\n"
    assert SnapshotStore(store.state_home).load(root) == ({"app.py": "original\n"}, {})
    source.write_text("newer\n", encoding="utf-8")
    assert store.put_baseline(root, "app.py", "newer\n") == "original\n"
