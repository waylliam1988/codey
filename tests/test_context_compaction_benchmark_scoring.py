"""A/B scoring uses exact facts and cannot manufacture gains from missing data."""
from tools.context_compaction_benchmark.scorer import score_answer, score_trial, verdict


def test_constraint_evidence_and_correction_are_separate_exact_checks():
    expected = {"constraint": "no-db", "target": "app.py", "exit_code": 1, "result_ref": "exec-17"}
    row = score_answer('{"constraint":"no-db","target":"wrong.py","exit_code":0,"result_ref":"exec-17"}', expected)
    assert row == {"constraints": True, "latest_correction": False, "evidence": False, "result_reference": True, "success": False}
    assert not score_answer("All tests passed", expected)["success"]


def test_incomplete_experiments_and_missing_cost_never_claim_a_cost_gain():
    assert verdict([], []) == "inconclusive"
    base = {'case': 'fixture', 'seed': 41}
    a = [base | {"success": True, "total_tokens": None}]
    assert verdict(a, a) == "no_gain"
    assert verdict(a, [base | {"success": True, "total_tokens": 20}]) == "no_gain"
    assert verdict(a, [base | {"success": False, "total_tokens": 0}]) == "regression"
    assert verdict([{'success': True}], [{'success': True}]) == 'inconclusive'


def test_quality_gain_with_higher_cost_reports_the_tradeoff():
    base = {'case': 'fixture', 'seed': 41}
    assert verdict([base | {"success": False, "total_tokens": 10}],
                   [base | {"success": True, "total_tokens": 20}]) == "quality_gain_with_cost"


def test_an_earlier_correct_answer_cannot_hide_a_later_replay_failure():
    expected = {"constraint":"no-db", "target":"app.py", "exit_code":1, "result_ref":"exec-17"}
    row = {'answer':'{"constraint":"no-db","target":"app.py","exit_code":1,"result_ref":"exec-17"}',
           'success':False, 'failure_kind':'context_overflow'}
    assert not score_trial(row, expected)['success']


def test_four_facts_cannot_hide_lost_middle_and_tail_observations():
    import json

    expected = {"constraint": "no-db", "target": "app.py", "exit_code": 1, "result_ref": "exec-17",
                "observations": {"middle": "unique-17", "tail": "unique-35"}}
    incomplete = {key: value for key, value in expected.items() if key != "observations"}
    assert not score_answer(json.dumps(incomplete), expected)["success"]
    wrong = expected | {"observations": {"middle": "unique-17", "tail": "guessed"}}
    assert not score_answer(json.dumps(wrong), expected)["success"]
    assert score_answer(json.dumps(expected), expected)["success"]


def test_one_improved_case_cannot_hide_a_regression_in_another_case():
    before = [{'case': 'recovery', 'seed': 41, 'success': True, 'total_tokens': 100},
              {'case': 'growth', 'seed': 41, 'success': False, 'total_tokens': 100}]
    after = [{'case': 'recovery', 'seed': 41, 'success': False, 'total_tokens': 50},
             {'case': 'growth', 'seed': 41, 'success': True, 'total_tokens': 50}]
    assert verdict(before, after) == 'regression'
