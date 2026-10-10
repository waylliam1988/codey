"""Project-kernel recovery requires actual current checks, behavior and review."""
import pytest

from tests.manual.candidate_validation_and_behavioral_repair_experiment import (
    GOOD,
    INITIAL,
    REUSE_FACTS,
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


def test_behavior_failure_repair_reuses_actual_counterexample_and_original_check(tmp_path):
    result = scenario(tmp_path, kind='behavioral', fault='needs_guidance')
    assert_complete(result)
    assert REUSE_FACTS in result['calls'][1].task
    assert not any(ev['tool'] == 'shell' for ev in result['events'])
    observations = [r for r in result['ledger'] if r['type'] == 'behavioral_observed']
    assert [r['status'] for r in observations] == ['fail', 'pass']
    assert observations[0]['workspace_fingerprint'] != observations[1]['workspace_fingerprint']
    assert result['reviews'][-1]['source'] == GOOD


def test_repair_submission_refreshes_old_failure_instead_of_repeating_current_green_tests(tmp_path):
    result = scenario(tmp_path, kind='behavioral', fault='await_behavior_refresh')
    assert_complete(result)
    assert len([ev for ev in result['events'] if ev['tool'] == 'run' and ev['ok']]) == 2


def test_behavior_repair_distinguishes_required_relation_from_actual_failed_outputs(tmp_path):
    result = scenario(tmp_path, kind='behavioral')
    assert_complete(result)
    projection = result['calls'][1].completion_repair_context
    assert 'Required relation: right_value == left_value.' in projection
    assert 'Required: clean_name("Q!r7t") == clean_name("Qr7t").' in projection
    assert 'not expected outputs' in projection
    assert 'qr7t' in projection and 'q-r7t' in projection
    assert 'required_right_value_given_left' in projection
    assert 'ordinary test results do not contain' in result['calls'][1].task


def test_saved_counterexample_labels_observed_values_and_the_required_relation(tmp_path):
    import json

    from tests.test_python_behavioral_probe_observes_properties_and_failures import BAD, execute

    observation, _, store = execute(tmp_path, BAD)
    assert observation.status == 'fail'
    text, _ = store.read_tool_output('session', 'run', observation.output_ref)
    evidence = json.loads(text)
    assert evidence.get('value_role') == 'actual'
    assert evidence.get('required_relation') == 'right_value == left_value'
    failed = next(row for row in evidence['rows'] if row['equal'] is False)
    assert failed['left_value'] == 'qr7t' and failed['right_value'] == 'q-r7t'
    assert failed.get('required_right_value_given_left') == failed['left_value']


@pytest.mark.parametrize('fault', ['missing_tests', 'missing_behavior', 'missing_review', 'stale_review',
                                  'rejected_review', 'ignore_guidance'])
def test_repair_requires_new_tests_behavior_and_approved_current_review(tmp_path, fault):
    result = scenario(tmp_path, kind='behavioral', fault=fault)
    assert result['terminal']['stop_reason'] not in {'done', 'error', 'provider_failure'}
    if fault == 'ignore_guidance':
        assert result['terminal']['stop_reason'] == 'approval'
