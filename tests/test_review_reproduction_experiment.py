"""Real isolated execution and independent quality checks for review experiments."""
import pytest

from tests.manual.coding_pipeline_value_ab import grade, write_files
from tests.manual.reproducible_review_adapter import execute_probe
from tests.manual.review_reproduction_ab import CASES, candidates


def probe(body):
    return {'requirement_quote': 'Return the input.', 'script':
        'import unittest\nfrom app import identity\nclass Check(unittest.TestCase):\n'
        '    def test_contract(self):\n' + ''.join('        ' + line + '\n' for line in body.splitlines())}


@pytest.mark.parametrize('source,kind', [('def identity(x): return x\n', 'passed'),
    ('def identity(x): return 0\n', 'assertion_failed')])
def test_real_unittest_results_remain_observations_not_semantic_admission(tmp_path, source, kind):
    (tmp_path / 'app.py').write_text(source, encoding='utf-8')
    result = execute_probe(tmp_path, probe('self.assertEqual(identity(5), 5)'), authorized=True)
    assert result['observation_kind'] == kind and result['tests_run'] == 1
    assert result['workspace_unchanged'] and result['semantic_admission'] == 'unverified'
    assert 'bug_confirmed' not in result


def test_probe_writes_stay_in_disposable_project(tmp_path):
    (tmp_path / 'app.py').write_text('def identity(x): return x\n', encoding='utf-8')
    result = execute_probe(tmp_path, probe('from pathlib import Path\nPath("scratch.txt").write_text("scratch")\nself.assertEqual(identity(1),1)'), authorized=True)
    assert result['observation_kind'] == 'passed'
    assert not (tmp_path / 'scratch.txt').exists()


@pytest.mark.parametrize('script', ['import nonexistent_codey_probe_module\n', 'import unittest\n'])
def test_import_errors_and_zero_tests_are_not_valid_successful_checks(tmp_path, script):
    result = execute_probe(tmp_path, {'script': script, 'requirement_quote': 'task'}, authorized=True)
    assert result['observation_kind'] == 'execution_error'


def test_probe_execution_never_comes_from_the_model_request_alone(tmp_path):
    with pytest.raises(PermissionError):
        execute_probe(tmp_path, probe('pass'), authorized=False)


@pytest.mark.parametrize('name', list(CASES))
def test_candidate_controls_pass_but_seeded_defects_fail_independent_checks(tmp_path, name):
    case = CASES[name]
    write_files(tmp_path, case.files)
    write_files(tmp_path, candidates(name, False))
    assert not grade(tmp_path, case)['quality_passed']
    write_files(tmp_path, candidates(name, True))
    result = grade(tmp_path, case)
    assert result['quality_passed'] and result['scope_ok']
