"""Holdout contracts change the callable and data, keeping independent scoring."""
from codey.operations.behavioral_verification import prepare_behavioral_plan
from tests.manual.agent_stability_cases import fixture, run_verification
from tests.manual.behavioral_verification_local_holdout_ab import holdout_cases


def test_new_tasks_bind_a_different_callable_and_file_without_exposing_the_hidden_oracle(tmp_path):
    cases = holdout_cases()
    assert len(cases) == 2
    for case in cases:
        root = tmp_path / case.case_id
        fixture(root, case)
        plan = prepare_behavioral_plan(root, case.task)
        assert plan is not None and (plan.path, plan.function) == ('labels.py', 'squash_label')
        assert ' A.B 42! ' not in case.task and 'Qr7t' not in case.task
    assert cases[0].allowed_paths == ('labels.py',) and cases[1].allowed_paths == ()


def test_correct_control_matches_the_unchanged_oracle_and_wrong_control_passes_only_visible_tests(tmp_path):
    cases = holdout_cases()
    assert len(cases) == 2
    for case in cases:
        root = tmp_path / case.case_id
        fixture(root, case)
        observed = run_verification(root, case)
        assert observed['visible_tests']
        assert observed['hidden_checks'] is (case.allowed_paths == ())
