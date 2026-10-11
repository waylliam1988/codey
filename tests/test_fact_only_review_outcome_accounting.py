"""Partial review studies must preserve unknown results and generation costs."""
from tests.manual.fact_only_review_outcome_audit import analyze


def usage(value, complete):
    return {'known_token_usage': value, 'usage_complete': complete}


def test_interrupted_repair_is_unknown_instead_of_completed_or_failed():
    row = {'stage': 'repair', 'case': 'case', 'seed': 92, 'arm': 'codey',
           'seconds': None, 'success': None, 'usage': usage(None, False),
           'error': 'backend disconnected during user stop'}
    result = analyze({'runs': [row]}, {'runs': []})
    assert result['repair_details'][0]['available'] is False
    assert result['repair_details'][0]['success'] is None
    assert result['repair_details'][0]['false_completion'] is None
    assert result['incremental_costs']['repair']['codey']['total_tokens'] is None


def test_unknown_shared_proposal_cost_is_not_zero_even_if_final_review_completed():
    rows = [
        {'stage': 'proposal', 'case': 'case', 'seed': 92, 'arm': 'plan-native',
         'seconds': None, 'usage': usage(None, False)},
        {'stage': 'semantic', 'case': 'case', 'seed': 92, 'arm': 'fact-only',
         'seconds': 2, 'usage': usage(25, True), 'evidence': []},
    ]
    result = analyze({'runs': rows}, {'runs': []})
    cost = result['incremental_costs']['semantic']['fact-only']
    assert cost['known_tokens'] == 25
    assert cost['total_tokens'] is None and cost['usage_complete'] is False
