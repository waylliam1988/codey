"""A review follow-up consumes the remaining task budget, never a fresh one."""
from types import SimpleNamespace
from unittest.mock import Mock

from codey.agents.writer_failover import CheckpointView
from codey.operations import project_review_phase
from codey.runtime.core.run_result import RunResult


def context(used, maximum=20):
    runner = SimpleNamespace(provider=object(), provider_id='local', switches=0,
                             run=Mock(return_value=RunResult('Checked', 'done', 2)))
    return SimpleNamespace(failover=runner, result=RunResult('Candidate', 'done', used),
        behavioral_plan=None, behavioral_observation=None,
        request=SimpleNamespace(max_turns=maximum), deps=SimpleNamespace(review=SimpleNamespace(review_fix_turns=50)),
        frame=SimpleNamespace(provider_id='local'), writer_attempt_index=1)


def test_review_repair_shares_remaining_budget_and_settles_total_turns(monkeypatch):
    ctx = context(17)
    mutations = SimpleNamespace(mark_writer_running=Mock(), mark_writer_settled=Mock())
    monkeypatch.setattr(project_review_phase, 'commit_runtime_operation',
                        lambda ctx, step, commit: commit(mutations, 's', 'r'))
    result = project_review_phase._repair_writer(ctx, 'Review follow-up', CheckpointView())
    assert ctx.failover.run.call_args.kwargs['turn_budget'] == 3
    assert result.turns == 19
    assert mutations.mark_writer_settled.call_args.kwargs['turns_used'] == 19


def test_exhausted_review_repair_cannot_restart_writer_or_get_a_new_budget(monkeypatch):
    ctx = context(20)
    commit = Mock()
    monkeypatch.setattr(project_review_phase, 'commit_runtime_operation', commit)
    result = project_review_phase._repair_writer(ctx, 'Review follow-up', CheckpointView())
    assert result.stop_reason == 'max_turns' and result.turns == 20
    commit.assert_not_called()
    ctx.failover.run.assert_not_called()
