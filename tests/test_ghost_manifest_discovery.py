"""Ordinary discovery and explicit recovery after a lost deletion response."""

from __future__ import annotations

import hashlib
import os
import subprocess
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

from codey.ghost.control_surface import GhostControlSurface
from codey.ghost.deletion_manifest import DeletionManifest
from codey.ghost.deletion_manifest_coordinator import DeletionManifestCoordinator
from codey.ghost.payload_types import mapping_fields
from tests.manual.ghost_memory_routing.deletion_runtime_probe import _seed


def _runtime_digest() -> str:
    digest = hashlib.sha256()
    for source in sorted(Path("codey").rglob("*.py")):
        digest.update(source.as_posix().encode())
        digest.update(source.read_bytes())
    return digest.hexdigest()


def _crash(home: Path, window: str) -> None:
    """Called in a fresh process; no response or operation ID reaches parent."""
    surface = GhostControlSurface.from_state_home(home)
    request: dict[str, object] = {"action": "delete_scope", "scope": "project", "project": str(home / "project"), "confirm": True}
    if window == "started":
        def started(coordinator: DeletionManifestCoordinator, manifest: DeletionManifest) -> DeletionManifest:
            os._exit(73)

        with patch.object(DeletionManifestCoordinator, "run", started):
            surface.dispatch_action(request)
    elif window == "between_stores":
        assert surface.hebbian is not None

        def second_store(scope: str, *, project: str = "", session_id: str = "") -> object:
            os._exit(73)

        with patch.object(surface.hebbian, "delete_scope", second_store):
            surface.dispatch_action(request)
    elif window == "before_response":
        original_run = DeletionManifestCoordinator.run

        def completed(coordinator: DeletionManifestCoordinator, manifest: DeletionManifest) -> DeletionManifest:
            original_run(coordinator, manifest)
            os._exit(73)

        with patch.object(DeletionManifestCoordinator, "run", completed):
            surface.dispatch_action(request)
    else:
        raise ValueError(window)
    raise AssertionError("crash hook was not reached")


def _items(payload: object) -> list[dict[str, object]]:
    assert mapping_fields(payload)
    rows = payload["items"]
    assert isinstance(rows, list)
    values: list[object] = rows
    result: list[dict[str, object]] = []
    for value in values:
        assert mapping_fields(value)
        result.append(dict(value))
    return result


@pytest.mark.parametrize("window", ["started", "between_stores", "before_response"])
def test_lost_response_is_discoverable_and_recoverable_without_database_queries(tmp_path: Path, window: str) -> None:
    surface = GhostControlSurface.from_state_home(tmp_path)
    project = str(tmp_path / "project")
    _seed(surface, project)
    before = _runtime_digest()
    script = "from pathlib import Path; import sys; from tests.test_ghost_manifest_discovery import _crash; _crash(Path(sys.argv[1]), sys.argv[2])"
    crashed = subprocess.run([sys.executable, "-c", script, str(tmp_path), window], capture_output=True, text=True, timeout=30)
    assert crashed.returncode == 73, crashed.stderr
    assert crashed.stdout == ""
    assert _runtime_digest() == before
    reopened = GhostControlSurface.from_state_home(tmp_path)
    exported = reopened.export_state()
    assert exported["ok"] is True
    rows = _items(exported["deletions"])
    assert len(rows) == 1
    operation = rows[0]
    assert operation["status"] == ("completed" if window == "before_response" else "pending")
    assert operation["project"] == project
    summary_rows = _items(reopened.summary(project=project)["deletions"])
    assert summary_rows == rows
    identity = operation["delete_operation_id"]
    assert isinstance(identity, str) and identity
    request: dict[str, object] = {"action": "delete_scope", "scope": "project", "project": project, "confirm": True, "delete_operation_id": identity}
    if window == "before_response":
        _seed(reopened, project, run_id="fresh")
    status, response = reopened.dispatch_action(request)
    assert status == 200, response
    assert DeletionManifest.from_payload(response["deletion_manifest"]).status == "completed"
    assert reopened.observations is not None
    assert len(reopened.observations.read_all()) == (1 if window == "before_response" else 0)
    after_rows = _items(reopened.export_state()["deletions"])
    assert after_rows[0]["delete_operation_id"] == identity
    assert after_rows[0]["status"] == "completed"
