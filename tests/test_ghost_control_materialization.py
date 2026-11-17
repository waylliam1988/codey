"""Single control updates preserve other legacy/default control values."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from codey.ghost.inbox import GHOST_CONTROL_NAMES, GhostInboxStore

PROFILES = ("missing", "legacy_on", "legacy_off", "partial_current")
SCOPES = ("user", "project", "session")


@pytest.mark.parametrize("profile", PROFILES)
@pytest.mark.parametrize("name", GHOST_CONTROL_NAMES)
@pytest.mark.parametrize("enabled", (True, False))
@pytest.mark.parametrize("scope", SCOPES)
def test_one_control_update_preserves_unselected_effective_controls(
    tmp_path: Path, profile: str, name: str, enabled: bool, scope: str,
) -> None:
    inbox = GhostInboxStore(tmp_path)
    project, session = str((tmp_path / "project").resolve()), "control-materialization-session"
    if profile != "missing":
        payload: dict[str, object] = {"schema_version": 1, "learning_enabled": profile != "legacy_off"}
        if profile == "partial_current":
            payload["run_maintenance"] = False
        with inbox.records.transaction():
            inbox.records.set_setting("learning", payload)
    before = inbox.controls(project=project, session_id=session)
    assert inbox.set_control(name, enabled, scope=scope, project=project, session_id=session)
    expected = {**before, name: enabled if scope == "user" else before[name] and enabled}
    after = inbox.controls(project=project, session_id=session)
    restarted = GhostInboxStore(tmp_path).controls(project=project, session_id=session)
    measurement: dict[str, object] = {
        "profile": profile, "name": name, "enabled": enabled, "scope": scope,
        "before": before, "expected": expected, "after": after, "restarted": restarted,
        "pass": after == expected and restarted == expected,
    }
    print("CONTROL_MATERIALIZATION_CELL " + json.dumps(measurement, sort_keys=True), flush=True)
    assert after == expected
    assert restarted == expected
