"""A stopped repair has the same bounded candidate closure as the first writer."""
import pytest

from tests.manual.candidate_validation_and_behavioral_repair_experiment import assert_complete, scenario


def test_correct_repair_after_stale_searches_gets_current_tests_behavior_review_and_proof(tmp_path):
    result = scenario(tmp_path, kind='behavioral', repair_stagnates=True)
    assert_complete(result)
    assert len(result['calls']) == 3
    assert result['calls'][-1].provider.name == 'runtime candidate validation'
    assert len([r for r in result['ledger'] if r['type'] == 'candidate_validation_admitted']) == 1
    assert [r['status'] for r in result['ledger'] if r['type'] == 'behavioral_observed'] == ['fail', 'pass']


@pytest.mark.parametrize('fault,budget', [('missing_tests', 20), ('missing_review', 20),
    ('stale_review', 20), ('rejected_review', 20), ('missing_behavior', 20), ('cancel_repair', 20), ('', 13)])
def test_stagnant_repair_validation_respects_current_evidence_cancel_and_shared_budget(tmp_path, fault, budget):
    result = scenario(tmp_path, kind='behavioral', repair_stagnates=True, fault=fault, max_turns=budget)
    # A missing writer check is allowed to be obtained by runtime-owned
    # validation; the other boundaries must still prevent completion.
    if fault == 'missing_tests':
        assert_complete(result)
    else:
        assert result['terminal']['stop_reason'] not in {'done', 'error', 'provider_failure'}
    assert result['terminal']['turns'] <= budget


def test_wrong_stagnant_repair_still_cannot_complete(tmp_path):
    result = scenario(tmp_path, kind='behavioral', repair_stagnates=True,
                      source="def clean_name(text): return ''\n")
    assert result['terminal']['stop_reason'] not in {'done', 'error', 'provider_failure'}
