from __future__ import annotations

from tests.manual import research_fulltext_recovery_ab as experiment


def test_two_b_has_eight_eligible_fulltext_cases_and_two_controls() -> None:
    cases = experiment.cases()
    assert len(cases) == 12
    assert sum(case.needs_fulltext for case in cases) == 10
    assert sum(not case.needs_fulltext for case in cases) == 2


def test_recovery_preserves_identity_and_falls_back() -> None:
    summary = experiment.summarize()
    assert summary["mechanism"] == {
        "eligible_cases": 10,
        "recovered": 8,
        "identity_preserved": True,
        "fallback_preserved": True,
    }
