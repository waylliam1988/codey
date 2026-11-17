"""Ordinary tombstone reset must retain the original domain audit history."""

from __future__ import annotations

from pathlib import Path

import pytest

from codey.ghost.control_surface import GhostControlSurface
from codey.ghost.deletion_manifest import DeletionManifest
from tests.test_ghost_scope_history_retention import STORES, _history, _rebuild, _seed_history, _visible


@pytest.mark.parametrize("store", STORES)
@pytest.mark.parametrize("scope", ["user", "project", "session"])
def test_read_only_reset_control_keeps_original_history(tmp_path: Path, store: str, scope: str) -> None:
    surface = _seed_history(tmp_path, scope)
    original = _history(surface, store)
    assert original and _visible(surface, store), "invalid reset fixture"
    reopened = GhostControlSurface.from_state_home(tmp_path)
    assert _visible(reopened, store)
    assert _history(reopened, store) == original


@pytest.mark.parametrize("store", STORES)
@pytest.mark.parametrize("scope", ["user", "project", "session"])
@pytest.mark.parametrize("rebuild", [False, True])
def test_ordinary_reset_preserves_original_domain_history(
    tmp_path: Path, store: str, scope: str, rebuild: bool,
) -> None:
    surface = _seed_history(tmp_path, scope)
    original = _history(surface, store)
    assert original and _visible(surface, store), "invalid reset fixture"
    status, payload = surface.dispatch_action({
        "action": "reset_all", "confirm": True, "delete_operation_id": "history-reset",
    })
    assert status == 200, payload
    manifest = DeletionManifest.from_payload(payload["deletion_manifest"])
    assert manifest.status == "completed" and manifest.retention_class == "tombstone"
    assert not _visible(surface, store), "reset did not hide the original source"
    reopened = GhostControlSurface.from_state_home(tmp_path)
    if rebuild:
        _rebuild(reopened)
    assert not _visible(reopened, store), "reset source returned after reopen/rebuild"
    assert _history(reopened, store)[:len(original)] == original, "tombstone reset rewrote original domain history"
