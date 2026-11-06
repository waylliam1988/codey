from __future__ import annotations

from tests.manual import research_source_discovery_live_ab as experiment


def test_real_2a_contract_has_twelve_distinct_tasks_and_two_arms() -> None:
    cases = experiment.formal_cases()
    assert len(cases) == 12
    assert len({case.name for case in cases}) == 12
    assert experiment.ARMS == ("baseline", "browser_after_connector")
    assert sum(case.target_hosts == ("pubmed.ncbi.nlm.nih.gov",) for case in cases) == 6
    assert sum(case.target_hosts == ("arxiv.org",) for case in cases) == 6


def test_live_harness_self_test() -> None:
    assert len(experiment.formal_cases()) == 12
