"""Real fixed-worker executions, identity, authorization and honest error categories."""
from dataclasses import replace

import pytest

from codey.completion.behavioral_checks import admit_behavioral_plan
from codey.operations.behavioral_verification import prepare_behavioral_plan, run_behavioral_probe
from codey.policies.task_policy import TaskPolicy
from codey.storage.managed_outputs import ManagedOutputStore

TASK = 'In names.py, clean_name removes ASCII punctuation and preserves digits.'
GOOD = "import string\ndef clean_name(text):\n    return text.translate(str.maketrans('', '', string.punctuation)).lower()\n"
BAD = "import string\ndef clean_name(text):\n    return '-'.join(''.join(' ' if c in string.punctuation else c for c in text).lower().split())\n"


def execute(tmp_path, source, *, task=TASK, grants=('project.read', 'project.verify'), timeout=5):
    project = tmp_path / 'project'
    project.mkdir(exist_ok=True)
    (project / 'names.py').write_text(source)
    plan = admit_behavioral_plan(TASK, (('names.py', 'clean_name'),))
    assert plan is not None
    store = ManagedOutputStore(tmp_path / 'state')
    result = run_behavioral_probe(project, task, plan, policy=TaskPolicy(frozenset(grants)), store=store,
                                  session_id='session', run_id='run', timeout=timeout)
    return result, project, store


@pytest.mark.parametrize('source,status', [(GOOD, 'pass'), (BAD, 'fail')])
def test_real_probe_distinguishes_deletion_without_mutating_source_and_retains_receipt(tmp_path, source, status):
    result, project, store = execute(tmp_path, source)
    assert result.status == status
    assert result.workspace_fingerprint
    assert result.output_ref
    text, meta = store.read_tool_output('session', 'run', result.output_ref)
    assert 'Qr7t' in text and 'Q.r7t' in text
    assert meta['tool_name'] == 'behavioral_verification'
    assert (project / 'names.py').read_text() == source
    assert not (project / '__pycache__').exists()
    if status == 'fail':
        assert 'q-r7t' in result.summary and 'qr7t' in result.summary


def test_freeze_binds_request_and_permission_denial_never_runs_project_code(tmp_path):
    source = "raise RuntimeError('must not import')\ndef clean_name(text): return text\n"
    result, _, _ = execute(tmp_path, source, grants=('project.read',))
    assert result.status == 'not_run' and result.reason == 'permission_denied'
    result, _, _ = execute(tmp_path, source, task=TASK + ' New requirement.')
    assert result.status == 'not_run' and result.reason == 'task_changed'


@pytest.mark.parametrize('source,reason', [
    ("import absent_probe_dependency\ndef clean_name(text): return text\n", 'dependency_missing'),
    ("def different_name(text): return text\n", 'binding_invalid'),
    ("async def clean_name(text): return text\n", 'binding_invalid'),
])
def test_binding_and_import_failures_do_not_claim_business_counterexamples(tmp_path, source, reason):
    result, _, _ = execute(tmp_path, source)
    assert result.status == 'not_run' and result.reason == reason


def test_target_exception_on_supported_input_is_observed_failure(tmp_path):
    result, _, _ = execute(tmp_path, "def clean_name(text): raise ValueError('bad input')\n")
    assert result.status == 'fail' and result.reason == 'target_exception'
    assert 'ValueError' in result.summary


def test_timeout_remains_unverified_not_automatic_product_or_environment_failure(tmp_path):
    result, _, _ = execute(tmp_path, "def clean_name(text):\n    while True: pass\n", timeout=.25)
    assert result.status == 'not_run' and result.reason == 'timeout'


def test_binding_is_unique_and_supported_signature_is_checked_without_executing_module(tmp_path):
    (tmp_path / 'names.py').write_text(GOOD)
    plan = prepare_behavioral_plan(tmp_path, TASK)
    assert plan is not None and plan.function == 'clean_name'
    (tmp_path / 'names.py').write_text('async def clean_name(text): return text\n')
    assert prepare_behavioral_plan(tmp_path, TASK) is None


def test_path_escape_is_rejected_before_execution(tmp_path):
    (tmp_path / 'names.py').write_text(GOOD)
    plan = admit_behavioral_plan(TASK, (('names.py', 'clean_name'),))
    assert plan is not None
    result = run_behavioral_probe(tmp_path, TASK, replace(plan, path='../names.py'),
                                  policy=TaskPolicy(frozenset({'project.read', 'project.verify'})),
                                  store=ManagedOutputStore(tmp_path / 'state'), session_id='s', run_id='r')
    assert result.status == 'not_run' and result.reason == 'plan_invalid'


def test_non_json_return_is_unsupported_binding_not_a_business_failure(tmp_path):
    result, _, _ = execute(tmp_path, 'def clean_name(text): return {text}\n')
    assert result.status == 'not_run' and result.reason == 'unsupported_return'


def test_plain_function_in_a_module_using_standard_dataclass_import_semantics_remains_supported(tmp_path):
    source = ("from __future__ import annotations\nimport string\nfrom dataclasses import dataclass\n"
              "@dataclass\nclass Text:\n    value: str\n"
              "def clean_name(text):\n    return Text(text.translate(str.maketrans('', '', string.punctuation))).value\n")
    result, _, _ = execute(tmp_path, source)
    assert result.status == 'pass'


@pytest.mark.parametrize('suffix', ["assert __name__ == 'names'\n",
                                  'import names\nassert names.clean_name is clean_name\n'])
def test_probe_preserves_normal_module_identity_and_self_import_without_duplicate_initialization(tmp_path, suffix):
    result, _, _ = execute(tmp_path, GOOD + suffix)
    assert result.status == 'pass'
