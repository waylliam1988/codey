"""The existing completion gate must consume the complete admitted property result."""
from dataclasses import replace

import pytest

from codey.completion.behavioral_checks import BehavioralObservation, admit_behavioral_plan, behavioral_completion_check
from codey.operations.behavioral_verification import run_behavioral_probe
from codey.operations.completion_gate import evaluate
from codey.operations.task_session import TaskSession
from codey.policies.task_policy import TaskPolicy
from codey.storage.managed_outputs import ManagedOutputStore
from codey.workspace.revision import workspace_fingerprint

TASK = 'Verify names.py: clean_name removes ASCII punctuation.'


@pytest.mark.parametrize('status,want', [('pass', 'pass'), ('fail', 'fail'), ('not_run', 'not_run')])
def test_one_summary_check_preserves_failure_even_when_case_count_exceeds_proof_capacity(status, want):
    plan = admit_behavioral_plan(TASK, (('names.py', 'clean_name'),))
    assert plan is not None and len(plan.pairs) > 12
    result = BehavioralObservation(plan.digest, 'a' * 64, status, 'property_mismatch', 'out_0001_aaaaaaaaaaaa')
    row = behavioral_completion_check(plan, result, TASK, 'a' * 64)
    assert row is not None and row.check_id == 'behavioral_verification' and row.status == want


@pytest.mark.parametrize('change', ['task', 'code', 'plan', 'receipt'])
def test_old_pass_cannot_prove_a_different_request_candidate_plan_or_missing_execution(change):
    plan = admit_behavioral_plan(TASK, (('names.py', 'clean_name'),))
    result = BehavioralObservation(plan.digest, 'a' * 64, 'pass', 'matched', 'out_0001_aaaaaaaaaaaa')
    task, fingerprint = TASK, 'a' * 64
    if change == 'task':
        task += ' Changed meaning.'
    if change == 'code':
        fingerprint = 'b' * 64
    if change == 'plan':
        result = replace(result, plan_digest='b' * 64)
    if change == 'receipt':
        result = replace(result, output_ref='')
    row = behavioral_completion_check(plan, result, task, fingerprint)
    assert row is not None and row.status == 'not_run'


def test_unchanged_verification_task_with_approved_review_is_blocked_by_real_counterexample(tmp_path):
    (tmp_path / 'names.py').write_text("import string\ndef clean_name(text):\n    return '-'.join(''.join(' ' if c in string.punctuation else c for c in text).lower().split())\n")
    plan = admit_behavioral_plan(TASK, (('names.py', 'clean_name'),))
    policy = TaskPolicy(frozenset({'project.read', 'project.verify'}))
    result = run_behavioral_probe(tmp_path, TASK, plan, policy=policy,
                                  store=ManagedOutputStore(tmp_path.parent / (tmp_path.name + '-state')),
                                  session_id='s', run_id='r')
    session = TaskSession(task_kind='project', task_text=TASK, project=str(tmp_path), policy=policy)
    verdict = evaluate(session, 'Existing tests passed; reviewer approved.', context={
        'task': TASK, 'project': str(tmp_path), 'task_changed': False, 'behavioral_plan': plan,
        'behavioral_observation': result, 'behavioral_fingerprint': workspace_fingerprint(tmp_path)})
    assert not verdict.complete
    assert verdict.proof is not None
    assert any(c.check_id == 'behavioral_verification' and c.status == 'fail' for c in verdict.proof.checks)
    assert 'behavioral_plan:' + plan.digest in verdict.proof.external_refs
    assert result.output_ref in verdict.proof.external_refs
