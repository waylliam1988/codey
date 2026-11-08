from __future__ import annotations

import hashlib
import json
from contextlib import AbstractContextManager, nullcontext
from pathlib import Path

import pytest

from codey.storage.atomic_io import filesystem_path
from codey.storage.managed_outputs import ManagedJsonArtifactRef, ManagedOutputStore


def test_json_artifact_read_uses_writer_lock_for_paired_files(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    store = ManagedOutputStore(tmp_path)
    ref = store.write_json_artifact(session_id="s", run_id="r", payload={"value": 1})
    locks: list[Path] = []

    def observe_lock(path: Path) -> AbstractContextManager[None]:
        locks.append(path)
        return nullcontext()

    monkeypatch.setattr("codey.storage.managed_outputs.with_file_lock", observe_lock)
    assert store.read_json_artifact(ref) == {"value": 1}
    assert locks == [filesystem_path(store._run_dir("s", "r"))]


def test_json_artifact_round_trip_supports_long_windows_paths(tmp_path: Path) -> None:
    root = tmp_path / ("nested-" + "x" * 180)
    store = ManagedOutputStore(root)
    ref = store.write_json_artifact(session_id="s", run_id="r", payload={"value": 2})
    assert len(str(store._handle_path("s", "r", ref.handle, ".meta.json"))) > 240
    assert store.read_json_artifact(ref) == {"value": 2}


def test_same_run_id_in_different_sessions_has_distinct_artifact_handles(tmp_path: Path) -> None:
    store = ManagedOutputStore(tmp_path)
    first = store.write_json_artifact(session_id="first", run_id="r", payload={"value": 1})
    second = store.write_json_artifact(session_id="second", run_id="r", payload={"value": 1})
    assert first.handle != second.handle
    assert first.sha256 == second.sha256
    assert store.read_json_artifact(first) == store.read_json_artifact(second) == {"value": 1}


def test_legacy_managed_handles_remain_readable(tmp_path: Path) -> None:
    store = ManagedOutputStore(tmp_path)
    text = "Existing source body"
    digest = hashlib.sha256(text.encode()).hexdigest()
    handle = f"out_0001_{digest[:12]}"
    path = store.path_for("s", "r", handle)
    path.parent.mkdir(parents=True)
    path.write_text(text, encoding="utf-8")
    store.metadata_path_for("s", "r", handle).write_text(json.dumps({
        "schema_version": 1, "handle": handle, "sha256": digest, "stored_bytes": len(text.encode()),
    }), encoding="utf-8")
    assert store.read_tool_output("s", "r", handle)[0] == text
    assert store.durable_record(handle, session_id="s", run_id="r", owner_task_id="r").sha256 == digest

    raw = b'{"value":1}'
    digest = hashlib.sha256(raw).hexdigest()
    handle = f"artifact_{digest[:24]}"
    ref = ManagedJsonArtifactRef("s", "r", handle, digest, 1)
    store._handle_path("s", "r", handle, ".json").write_bytes(raw)
    store._handle_path("s", "r", handle, ".meta.json").write_text(json.dumps({
        "schema_version": 1, "session_id": "s", "run_id": "r", "handle": handle, "sha256": digest,
    }), encoding="utf-8")
    assert store.read_json_artifact(ref) == {"value": 1}
