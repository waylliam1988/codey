"""Project recovery requires current registered checks, review and completion proof."""
import pytest

from tests.project_candidate_recovery_fixture import (
    INITIAL,
    VALIDATE_TASK,
    assert_complete,
    scenario,
)


def test_correct_stagnant_candidate_gets_fresh_verification_review_and_completion(tmp_path):
    result = scenario(tmp_path)
    assert len([ev for ev in result['events'] if ev['tool'] == 'edit' and not ev['ok']]) == 3
    assert_complete(result)
    assert len(result['calls']) == 2 and len(result['reviews']) == 1


def test_runtime_owned_validation_executes_only_the_admitted_check_through_real_kernel(tmp_path):
    result = scenario(tmp_path, fault='runtime_validation')
    assert_complete(result)
    resumed = result['calls'][1]
    assert resumed.max_turns == 2
    assert resumed.provider.name == 'runtime candidate validation'
    assert result['terminal']['turns'] == 9
    assert len([ev for ev in result['events'] if ev['tool'] == 'edit']) == 4


@pytest.mark.parametrize('fault,budget', [('cancel', 20), ('deny_project.verify', 20),
                                        ('deny_project.write', 20), ('', 7)])
def test_stopped_candidate_cannot_override_cancel_permissions_or_turn_budget(tmp_path, fault, budget):
    result = scenario(tmp_path, fault=fault, max_turns=budget)
    assert result['terminal']['stop_reason'] not in {'done', 'error', 'provider_failure'}
    assert len(result['calls']) == 1 and not result['reviews']
    if fault == 'deny_project.write':
        assert (result['project'] / 'names.py').read_text() == INITIAL


def test_wrong_stagnant_candidate_cannot_borrow_false_approval_as_success(tmp_path):
    result = scenario(tmp_path, source="def clean_name(text): return ''\n")
    assert result['terminal']['stop_reason'] not in {'done', 'error', 'provider_failure'}
    assert result['terminal']['receipt']['verification']['checks_passed'] is False


def test_failed_registered_check_is_repaired_with_new_validation_and_current_review(tmp_path):
    result = scenario(tmp_path, kind='repair')
    assert_complete(result)
    assert len(result['calls']) == 2 and len(result['reviews']) == 2
    assert result['reviews'][0]['source'] != result['reviews'][-1]['source']
    assert 'python -m unittest' in result['calls'][1].completion_repair_context


@pytest.mark.parametrize('fault', ['missing_verification', 'missing_review', 'stale_review', 'rejected_review'])
def test_repair_requires_current_tests_and_approved_current_review(tmp_path, fault):
    result = scenario(tmp_path, kind='repair', fault=fault)
    assert result['terminal']['stop_reason'] not in {'done', 'error', 'provider_failure'}
    if fault == 'missing_verification':
        assert len(result['calls']) == 3 and result['calls'][-1].task == VALIDATE_TASK
        assert not any(event['tool'] == 'run' and event['exit_code'] == 0 for event in result['events'])


def test_model_omitting_tests_completes_only_after_real_runtime_validation_and_review(tmp_path):
    result = scenario(tmp_path, kind='repair', fault='missing_tests')
    assert_complete(result)
    assert len(result['calls']) == 3 and result['calls'][-1].task == VALIDATE_TASK
    assert result['calls'][-1].provider.name == 'runtime candidate validation'
    assert len(result['reviews']) == 2
    assert result['reviews'][0]['source'] != result['reviews'][-1]['source']
    passed = [event for event in result['events'] if event['tool'] == 'run' and event['exit_code'] == 0]
    admitted = [row for row in result['ledger'] if row['type'] == 'candidate_validation_admitted']
    assert len(admitted) == 1 and admitted[0]['driver'] == 'runtime'
    assert len(passed) == 1 and passed[0]['command'] == admitted[0]['command']
