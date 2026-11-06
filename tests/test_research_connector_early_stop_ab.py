from __future__ import annotations

from tests.manual import research_connector_early_stop_ab as experiment


def test_two_arms_and_twelve_frozen_cases() -> None:
    assert experiment.ARMS == ("baseline", "browser_after_connector")
    assert len(experiment.cases()) == 12
    assert all(case.early_stop_expected for case in experiment.cases())


def test_baseline_stops_and_candidate_calls_browser() -> None:
    summary = experiment.summarize()
    assert summary["mechanism"] == {
        "baseline_early_stop": True,
        "candidate_browser_called": True,
        "candidate_adds_material": True,
        "limit_preserved": True,
    }
