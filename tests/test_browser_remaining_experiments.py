"""The remaining plan changes one candidate at a time and keeps controls."""
from __future__ import annotations

from tools.browser_remaining_experiments import plan


def test_remaining_plan_covers_unfinished_variables_and_projection_diagnostics() -> None:
    experiments = plan()
    diagnostics = [row for row in experiments if row.diagnostic]
    matrices = [row for row in experiments if not row.diagnostic]
    assert len(diagnostics) == 9
    assert {row.name for row in matrices} == {
        "delta-v2", "bounded-v1", "settle-v1", "query-v1", "followup-v1", "ranking-v1", "completion-v1",
    }
    assert sum(row.calls for row in experiments) == 171
    for row in matrices:
        assert "control_static" in row.arguments or "control_restricted" in row.arguments
        assert not ("--projection" in row.arguments and "--policy" in row.arguments)
        if row.name != "delta-v2":
            assert "--source-tools-only" in row.arguments
