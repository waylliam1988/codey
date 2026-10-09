"""Pairing, exclusions and comparable costs in the real-agent report."""

import pytest

from tests.manual import agent_stability_measurements as measurements


def arm(name, *, seed=41, success=True, status="completed", seconds=10, tokens=None):
    return {"arm": name, "seed": seed, "case": "fixture", "status": status,
            "wall_time_seconds": seconds, "metrics": {"scenario_success": success,
            "token_usage": tokens, "usage_complete": tokens is not None}}


def test_failed_arm_is_not_a_speed_win_and_missing_usage_is_not_zero():
    summary = measurements.paired_summary([arm("codey", seconds=20, tokens=10),
                                           arm("pi", success=False, seconds=2)])
    assert summary["pairs"][0]["outcome"] == "codey_only"
    assert summary["pairs"][0]["codey_time_delta_percent"] is None
    assert summary["pairs"][0]["codey_token_delta_percent"] is None


def test_only_both_successful_complete_pairs_get_cost_deltas():
    summary = measurements.paired_summary([arm("codey", seconds=8, tokens=0),
                                           arm("pi", seconds=10, tokens=20)])
    assert summary["pairs"][0]["codey_time_delta_percent"] == -20
    assert summary["pairs"][0]["codey_token_delta_percent"] == -100


def test_environment_errors_and_missing_arms_are_excluded_from_comparison():
    summary = measurements.paired_summary([arm("codey"), arm("pi", status="environment_error"),
                                           arm("codey", seed=42)])
    assert summary["comparable_pairs"] == 0
    assert len(summary["excluded_pairs"]) == 2


def test_duplicate_run_keys_require_an_explicit_choice_instead_of_overwriting():
    with pytest.raises(ValueError, match="duplicate"):
        measurements.paired_summary([arm("codey"), arm("codey"), arm("pi")])
