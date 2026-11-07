from __future__ import annotations

from tests.manual import research_hyperresearch_acquisition_ab as experiment


def test_contract_has_two_arms_and_bounded_candidate() -> None:
    assert experiment.ARMS == ("baseline", "multi_angle_gap")
    assert len(experiment.ACQUISITION_GUIDANCE) < 1200
    assert "primary or official source" in experiment.ACQUISITION_GUIDANCE
    assert "could change the answer" in experiment.ACQUISITION_GUIDANCE


def test_candidate_changes_only_question_guidance() -> None:
    baseline = experiment.question("arxiv_rag", "baseline")
    candidate = experiment.question("arxiv_rag", "multi_angle_gap")
    assert candidate.startswith(baseline)
    assert candidate != baseline
    assert experiment.question("arxiv_rag", "multi_angle_gap").count(experiment.ACQUISITION_GUIDANCE) == 1


def test_dev_cases_are_distinct_and_live_questions_exist() -> None:
    assert len(experiment.DEV_CASES) == 4
    assert len(set(experiment.DEV_CASES)) == 4
    assert all(case in experiment.live.QUESTIONS for case in experiment.DEV_CASES)
