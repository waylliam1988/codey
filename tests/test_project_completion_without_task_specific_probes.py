"""Project completion uses registered checks and review, without hidden task probes."""
import json
from dataclasses import fields
from importlib.util import find_spec
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from codey.operations import project_candidate_validation, project_review_phase
from codey.operations.project_completion_context import ProjectRun
from codey.operations.task_session import TaskSession
from codey.operations.tool_result_reader import read_tool_result
from codey.policies.task_policy import TaskPolicy
from codey.runtime.core.models import ToolCall
from codey.runtime.core.run_result import RunResult
from codey.storage.managed_outputs import ManagedOutputStore
from tests.project_candidate_recovery_fixture import scenario


def test_punctuation_requirement_has_only_registered_verification_and_current_review(tmp_path):
    result = scenario(tmp_path, fault='runtime_validation')
    assert result['terminal']['stop_reason'] == 'done'
    assert result['terminal']['receipt']['verification']['checks_passed'] is True
    assert result['reviews'] and result['proofs'][-1]['satisfied']
    assert not [row for row in result['ledger'] if row['type'] in
                {'behavioral_plan_admitted', 'behavioral_observed'}]
    assert 'behavioral_verification' not in json.dumps(result['proofs'])
    assert 'Qr7t' not in result['reviews'][-1]['facts']


def test_unindexed_archive_does_not_gain_receipt_authority_from_a_special_tool_name(tmp_path):
    store = ManagedOutputStore(tmp_path / 'outputs')
    ref = store.write_tool_output(session_id='s', run_id='r', tool_id='probe',
        permission_profile='coding_writer', tool_name='behavioral_verification', display_ref='.',
        text=json.dumps({'status': 'pass'}))
    assert ref is not None
    delegate = SimpleNamespace(session=TaskSession(policy=TaskPolicy(frozenset({'control'}))),
                               managed_outputs=store, session_id='s', run_id='r')
    result = read_tool_result(delegate, ToolCall('read_tool_result', {'result_ref': ref.handle}))
    assert not result.ok
    assert 'does not authorize re-execution' in result.model_text


def test_task_specific_modules_and_project_state_are_removed():
    for name in ('codey.completion.behavioral_checks', 'codey.operations.behavioral_verification',
                 'codey.operations.behavioral_probe_worker'):
        assert find_spec(name) is None
    assert not {'behavioral_plan', 'behavioral_observation'} & {field.name for field in fields(ProjectRun)}


def test_visible_checks_and_review_do_not_gain_an_implicit_punctuation_oracle(tmp_path):
    source = ("import string\ndef clean_name(text):\n"
              "    return '-'.join(''.join(' ' if c in string.punctuation else c for c in text).lower().split())\n")
    result = scenario(tmp_path, source=source, fault='runtime_validation')
    # This deliberately wrong semantic interpretation passes the fixture's
    # visible tests and scripted review. Removal is not a new quality guarantee.
    assert result['terminal']['stop_reason'] == 'done'
    assert result['proofs'][-1]['satisfied']
    assert 'behavioral_verification' not in json.dumps(result['proofs'])


@pytest.mark.parametrize('approved', [True, False])
def test_review_repair_still_requires_a_new_approved_current_review(monkeypatch, approved):
    ctx = SimpleNamespace(result=RunResult('Repaired', 'done', 5), task_changed=True,
                          work=SimpleNamespace(operation=None), repaired_once=False,
                          review_cycle=SimpleNamespace(review_repair_attempted=True,
                                                      approved_current_snapshot=False), blocked_reason='')
    monkeypatch.setattr(project_candidate_validation, 'validate_stopped_candidate', Mock())
    monkeypatch.setattr(project_candidate_validation, 'submit_readonly_completion_candidate', Mock())
    attempts = []

    def review(ctx, *, allow_repair):
        attempts.append(allow_repair)
        if not allow_repair:
            ctx.review_cycle.approved_current_snapshot = approved

    monkeypatch.setattr(project_review_phase, 'run_review_phase', review)
    project_review_phase.validate_candidate(ctx)
    assert attempts == [True, False]
    assert ctx.result.stop_reason == ('done' if approved else 'blocked')
    assert bool(ctx.blocked_reason) is (not approved)
