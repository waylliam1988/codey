"""Counterexample observations must remain distinct from semantic admission."""
import pytest

from tests.manual.counterexample_review_adapter import observe, online_manifest, validate_probe


def probe(**kw):
    return {'requirement_quote': 'Zero is a valid bound.', 'preconditions': 'integer value',
            'args': [-2], 'kwargs': {'low': 0}, 'expected': 0,
            'alternative': 'zero treated as absent', 'suggested_fix': 'check against None', **kw}


def test_a_real_quote_does_not_admit_the_expected_value_as_truth():
    result = validate_probe(probe(expected=123), 'Zero is a valid bound.')
    assert result['quote_present'] is True
    assert result['semantic_admission'] == 'unverified'
    assert 'passed' not in result and 'supported' not in result


@pytest.mark.parametrize('bad', [probe(requirement_quote='Invented requirement'),
    probe(args='__import__("os")'), probe(kwargs={'__code__': 'execute'}), probe(args=[float('nan')])])
def test_invalid_probes_cannot_be_executed(bad):
    with pytest.raises(ValueError):
        validate_probe(bad, 'Zero is a valid bound.')


def test_observation_requires_explicit_fixture_execution_authorization(tmp_path):
    (tmp_path / 'app.py').write_text('raise AssertionError("should not be imported")')
    with pytest.raises(PermissionError):
        observe(tmp_path, [probe()])


def test_actual_observation_does_not_compare_against_model_expectation(tmp_path):
    (tmp_path / 'app.py').write_text('def select(value, low=None): return value\n')
    result = observe(tmp_path, [probe(expected=123)], authorized=True)
    assert result['observations'][0]['value'] == -2
    assert result['observations'][0]['status'] == 'returned'
    assert 'passed' not in result['observations'][0]
    assert result['source_sha256'] == result['final_source_sha256']


def test_target_exceptions_are_observations_not_automatic_defects(tmp_path):
    (tmp_path / 'app.py').write_text('def select(*args, **kwargs): raise ValueError("outside domain")\n')
    row = observe(tmp_path, [probe()], authorized=True)['observations'][0]
    assert row['status'] == 'raised' and row['exception_type'] == 'ValueError'
    assert 'defect' not in row


def test_expected_values_and_hidden_rules_are_not_in_online_manifest():
    from types import SimpleNamespace
    case = SimpleNamespace(task='visible requirement', files={'app.py': 'source'}, candidate='patch',
                           oracle='HIDDEN ORACLE', reference='HIDDEN REFERENCE', buggy=True, cohort='holdout')
    spec = online_manifest(case)
    assert set(spec) == {'task', 'files', 'candidate'}
    assert 'HIDDEN' not in str(spec)


def test_offline_scoring_separates_wrong_expected_from_real_distinguishing_input():
    from tests.manual.counterexample_review_cases import cases, score_probe
    case = next(c for c in cases() if c.name == 'zero-bound-bug')
    result = score_probe(case, probe(expected=123), {'status': 'returned', 'value': -2})
    assert result['valid_domain'] and result['distinguishes']
    assert result['expected_correct'] is False


def test_outside_domain_counterexample_cannot_count_as_a_defect():
    from tests.manual.counterexample_review_cases import cases, score_probe
    case = next(c for c in cases() if c.name == 'ordered-range-bug')
    result = score_probe(case, probe(args=[5, 2], kwargs={}, expected=[]), {'status': 'returned', 'value': []})
    assert result == {'valid_domain': False, 'expected_correct': None, 'distinguishes': False}


def test_controls_and_bugs_share_visible_task_but_only_controls_pass_offline_reference(monkeypatch):
    import sys
    import types
    import unittest

    from codey.task.entry_auth import derive_entry_auth
    from tests.manual.counterexample_review_cases import cases
    fixtures = cases()
    assert len(fixtures) == 8 and sum(c.cohort == 'holdout' for c in fixtures) == 4
    for case in fixtures:
        module = types.ModuleType('app')
        exec(case.candidate, module.__dict__)
        monkeypatch.setitem(sys.modules, 'app', module)
        visible = types.ModuleType('visible')
        exec(case.files['test_app.py'], visible.__dict__)
        result = unittest.TestResult()
        unittest.defaultTestLoader.loadTestsFromModule(visible).run(result)
        assert result.wasSuccessful(), case.name
        if case.buggy:
            with pytest.raises(AssertionError):
                exec(case.oracle, {'m': module})
        else:
            exec(case.oracle, {'m': module})
        assert 'project.write' not in derive_entry_auth({'task': case.task, 'intent': 'project'},
                                                       project='fixture').denied_capabilities
        partner = next(c for c in fixtures if c.name.rsplit('-', 1)[0] == case.name.rsplit('-', 1)[0]
                       and c.buggy != case.buggy)
        assert partner.task == case.task and partner.files == case.files


def test_invalid_probe_does_not_synthesize_a_verdict_and_format_retry_preserves_history(tmp_path):
    import json

    from tests.manual.counterexample_review_adapter import CounterexampleReviewer
    class Delegate:
        def __init__(self):
            self.prompts = []
        def send(self, text, **kw):
            self.prompts.append(text)
            return (json.dumps({'hypotheses': [probe(requirement_quote='invented')]}) if len(self.prompts) == 1
                    else '{"verdict":"changes_requested","summary":"unresolved","findings":[]}')
    delegate = Delegate()
    reviewer = CounterexampleReviewer(delegate, tmp_path, 'Zero is a valid bound.', tmp_path / 'evidence.jsonl')
    reply = reviewer.send('original task and diff')
    assert json.loads(reply)['verdict'] == 'changes_requested'
    assert not (tmp_path / 'app.py').exists()  # Invalid input was never executed.
    reviewer.send('format only, preserve verdict')
    assert delegate.prompts[-1] == 'format only, preserve verdict'
    row = json.loads((tmp_path / 'evidence.jsonl').read_text())
    assert row['actual'] is None and len(row['invalid_probes']) == 1


def test_timeout_is_not_reported_as_a_semantic_failure(tmp_path):
    (tmp_path / 'app.py').write_text('def select(*args, **kwargs):\n    while True: pass\n')
    row = observe(tmp_path, [probe()], authorized=True)['observations'][0]
    assert row == {'status': 'timeout'}
