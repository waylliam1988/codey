"""A deliberately uncommitted or disabled memory update is not a failure."""

from types import SimpleNamespace

import pytest

from codey.operations.task_phases.settlement import _emit_observation_warning


@pytest.mark.parametrize("status", ["committed", "uncommitted", "skipped"])
def test_expected_observation_outcomes_do_not_emit_pause_warnings(status):
    events = []
    _emit_observation_warning(SimpleNamespace(emit=events.append), run_id="run", session_id="chat", status=status)
    assert not events


@pytest.mark.parametrize(
    "status,error_type", [("failed", "GhostObservationFailed"), ("settings_error", "GhostSettingsCorrupt")]
)
def test_real_observation_failures_keep_correlated_diagnostics(status, error_type):
    events = []
    _emit_observation_warning(SimpleNamespace(emit=events.append), run_id="run", session_id="chat", status=status)
    assert events[0]["type"] == "ghost_post_turn_warning"
    assert events[0]["error_type"] == error_type
    assert events[0]["run_id"] == "run"
    assert events[0]["session_id"] == "chat"
