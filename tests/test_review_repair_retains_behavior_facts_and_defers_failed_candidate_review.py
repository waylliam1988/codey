"""Actual behavior failures take the bounded repair path before another review."""
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from tests.manual.candidate_validation_and_behavioral_repair_experiment import (
    BAD,
    GOOD,
    assert_complete,
    scenario,
)


def test_review_regression_uses_fresh_counterexample_before_reviewing_repaired_candidate(tmp_path):
    result = scenario(tmp_path, fault='review_regression', max_turns=30)
    assert_complete(result)
    assert [r['status'] for r in result['ledger'] if r['type'] == 'behavioral_observed'] == ['pass', 'fail', 'pass']
    assert [r['source'] for r in result['reviews']] == [GOOD, GOOD]
    assert len([r for r in result['ledger'] if r['type'] == 'candidate_validation_admitted']) == 1
    assert 'required_right_value_given_left' in result['calls'][-1].completion_repair_context
    assert result['terminal']['turns'] <= 30


def test_review_writer_receives_current_actual_behavior_facts_without_new_probe(monkeypatch):
    from codey.agents.writer_failover import CheckpointView
    from codey.operations import project_review_phase
    from tests.test_review_repair_preserves_total_turn_budget import context

    ctx = context(17)
    ctx.behavioral_plan = SimpleNamespace(requirement_quote='An admitted relation', digest='plan', function='calculate')
    ctx.behavioral_observation = SimpleNamespace(workspace_fingerprint='fingerprint', status='pass',
        reason='matched', output_ref='out_0001_aaaaaaaaaaaa', summary='actual operands and outputs')
    mutations = SimpleNamespace(mark_writer_running=Mock(), mark_writer_settled=Mock())
    monkeypatch.setattr(project_review_phase, 'commit_runtime_operation',
                        lambda ctx, step, commit: commit(mutations, 's', 'r'))
    project_review_phase._repair_writer(ctx, 'Review follow-up', CheckpointView())
    task = ctx.failover.run.call_args.kwargs['task']
    assert 'actual operands and outputs' in task
    assert 'out_0001_aaaaaaaaaaaa' in task
    assert 'An admitted relation' in task
    assert 'calculate' in task and 'right_value == left_value' in task
    assert ctx.failover.run.call_args.kwargs['turn_budget'] == 3


@pytest.mark.parametrize('fault,source', [('missing_review', GOOD), ('stale_review', GOOD),
    ('rejected_review', GOOD), ('missing_behavior', GOOD), ('', BAD)])
def test_deferring_failed_review_never_replaces_fresh_final_evidence(tmp_path, fault, source):
    result = scenario(tmp_path, review_regression=True, fault=fault, source=source, max_turns=30)
    assert result['terminal']['stop_reason'] not in {'done', 'error', 'provider_failure'}
    assert not result['terminal']['receipt']['verification']['checks_passed']
    assert len([r for r in result['ledger'] if r['type'] == 'candidate_validation_admitted']) == 1
    assert result['terminal']['turns'] <= 30
