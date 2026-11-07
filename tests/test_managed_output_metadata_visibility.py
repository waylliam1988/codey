from __future__ import annotations

from contextlib import nullcontext

from codey.storage.atomic_io import filesystem_path
from codey.storage.managed_outputs import ManagedOutputStore


def test_json_artifact_read_uses_writer_lock_for_paired_files(tmp_path, monkeypatch) -> None:
    store = ManagedOutputStore(tmp_path)
    ref = store.write_json_artifact(session_id="s", run_id="r", payload={"value": 1})
    locks = []

    def observe_lock(path):
        locks.append(path)
        return nullcontext()

    monkeypatch.setattr("codey.storage.managed_outputs.with_file_lock", observe_lock)
    assert store.read_json_artifact(ref) == {"value": 1}
    assert locks == [filesystem_path(store._run_dir("s", "r"))]


def test_json_artifact_round_trip_supports_long_windows_paths(tmp_path) -> None:
    root = tmp_path / ("nested-" + "x" * 180)
    store = ManagedOutputStore(root)
    ref = store.write_json_artifact(session_id="s", run_id="r", payload={"value": 2})
    assert len(str(store._handle_path("s", "r", ref.handle, ".meta.json"))) > 240
    assert store.read_json_artifact(ref) == {"value": 2}
