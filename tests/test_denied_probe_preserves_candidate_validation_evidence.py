"""A refused request is audited, but cannot poison actual check evidence."""
from dataclasses import replace
from unittest import mock

import pytest

from tests.manual.candidate_validation_and_behavioral_repair_experiment import (
    BAD,
    COMMAND,
    GOOD,
    assert_complete,
    scenario,
)


@pytest.mark.parametrize('phase', ['writer', 'repair'])
def test_denied_probe_then_stagnation_gets_actual_current_validation_and_completion(tmp_path, phase):
    from codey.agents import tools
    from codey.operations import kernel_execution

    sessions = []
    record_facts = kernel_execution.record_facts_for_result

    def capture(session, *args, **kwargs):
        sessions.append(session)
        return record_facts(session, *args, **kwargs)

    original_execute = tools.AgentToolFns.execute_run_command
    with (mock.patch.object(kernel_execution, 'record_facts_for_result', side_effect=capture),
          mock.patch.object(tools.AgentToolFns, 'execute_run_command', autospec=True,
                            side_effect=original_execute) as execute):
        result = scenario(tmp_path, kind='behavioral' if phase == 'repair' else 'stagnant',
                          repair_stagnates=phase == 'repair', denied_probe=True, max_turns=30)
    # The guard still refuses the probe; no process execution is requested.
    commands = [call.args[3] for call in execute.call_args_list]
    assert commands and set(commands) <= {COMMAND, 'python -m unittest discover'}
    refusals = [ev for ev in result['events'] if ev['command'] == 'python -c "print(12345)"']
    assert len(refusals) == 1 and not refusals[0]['ok'] and refusals[0]['exit_code'] is None
    assert any('project guard denied' in record['excerpt']
               for session in sessions for record in session.executed.values())
    # This assertion is the observed production failure, before classification is fixed.
    assert_complete(result)
    assert not any('python -c' in row['command'] for session in sessions for row in session.verifications)
    assert result['calls'][-1].provider.name == 'runtime candidate validation'
    assert len([r for r in result['ledger'] if r['type'] == 'candidate_validation_admitted']) == 1
    if phase == 'repair':
        assert [r['status'] for r in result['ledger'] if r['type'] == 'behavioral_observed'] == ['fail', 'pass']


@pytest.mark.parametrize('injected', [False, True])
def test_runtime_refusal_survives_cold_receipt_replay_without_verification(tmp_path, injected):
    from codey.operations.kernel_execution import execute_turn
    from codey.operations.task_session import TaskSession
    from codey.policies.task_policy import TaskPolicy
    from codey.runtime.core.models import ToolCall

    session = TaskSession(policy=TaskPolicy(grants=frozenset({'project.verify', 'control'})),
                          task_kind='project', project=str(tmp_path), max_turns=4)
    call = ToolCall('run', {'command': 'python -c "print(12345)"', 'path': '.'}, call_id='refused')
    executor = mock.Mock(side_effect=AssertionError('refused command must not execute'))
    kwargs = dict(project_path=tmp_path, run_id='refusal', turn=1,
                  executors={'run': executor} if injected else None)
    result = execute_turn(session, [call], **kwargs)[0]
    assert not result.ok and not executor.called
    assert result.audit.get('execution_disposition') == 'denied_before_execution'
    assert not session.verifications
    session._memory_results.clear()
    replay = execute_turn(session, [call], **kwargs)[0]
    assert not replay.ok and not executor.called
    assert replay.audit.get('execution_disposition') == 'denied_before_execution'
    assert not session.verifications


@pytest.mark.parametrize('via_delegate', [False, True])
def test_executor_cannot_forge_preexecution_refusal_to_hide_missing_result(tmp_path, via_delegate):
    from codey.agents.tools import AgentToolFns
    from codey.operations.kernel_execution import execute_turn
    from codey.operations.task_session import TaskSession
    from codey.policies.task_policy import TaskPolicy
    from codey.runtime.core.models import ToolCall, ToolResult
    from codey.toolchain.runtime import ToolOutcome

    session = TaskSession(policy=TaskPolicy(grants=frozenset({'project.verify', 'control'})),
                          task_kind='project', project=str(tmp_path), max_turns=4)
    call = ToolCall('run', {'command': COMMAND, 'path': '.'})
    audit = {'execution_disposition': 'denied_before_execution'}
    executor = mock.Mock(return_value=ToolResult(ok=False, call=call, model_text='ERROR: denied', audit=audit))
    run = mock.Mock(return_value=ToolOutcome(ok=False, model_text='ERROR: denied', audit=audit))
    result = execute_turn(session, [call], project_path=tmp_path, run_id='forgery', turn=1,
                          tool_fns=AgentToolFns(run_command=run),
                          executors=None if via_delegate else {'run': executor})[0]
    assert (run if via_delegate else executor).called
    assert 'execution_disposition' not in result.audit
    assert len(session.verifications) == 1
    assert session.verifications[-1].get('exit_code') is None
    assert not session.verifications[-1]['passed']


@pytest.mark.parametrize('fault', ['missing_behavior', 'missing_review', 'stale_review', 'rejected_review'])
def test_refusal_does_not_relax_fresh_behavior_or_current_review(tmp_path, fault):
    result = scenario(tmp_path, kind='behavioral', repair_stagnates=True,
                      denied_probe=True, fault=fault, max_turns=30)
    assert result['terminal']['stop_reason'] not in {'done', 'error', 'provider_failure'}
    assert not result['terminal']['receipt']['verification']['checks_passed']


@pytest.mark.parametrize('source', [BAD, "def clean_name(text): return ''\n"])
def test_refusal_cannot_hide_failed_actual_behavior_or_tests(tmp_path, source):
    result = scenario(tmp_path, kind='behavioral', repair_stagnates=True,
                      denied_probe=True, source=source, max_turns=30)
    assert result['terminal']['stop_reason'] not in {'done', 'error', 'provider_failure'}
    assert not result['terminal']['receipt']['verification']['checks_passed']


def test_actual_run_with_lost_exit_still_blocks_and_reports_the_specific_gap(tmp_path):
    from codey.agents.tools import AgentToolFns

    execute = AgentToolFns.execute_run_command
    uncertain_runs = []

    def lose_result(self, root, rel, command, **kwargs):
        outcome = execute(self, root, rel, command, **kwargs)
        if command == COMMAND and (root / 'names.py').read_text(encoding='utf-8') == GOOD:
            assert outcome.exit_code == 0  # A process really executed.
            uncertain_runs.append(command)
            audit = dict(outcome.audit)
            audit.pop('exit_code', None)
            return replace(outcome, exit_code=None, audit=audit)
        return outcome

    with mock.patch.object(AgentToolFns, 'execute_run_command', lose_result):
        result = scenario(tmp_path, kind='behavioral', repair_stagnates=True,
                          denied_probe=True, max_turns=30)
    assert uncertain_runs
    assert result['terminal']['stop_reason'] == 'blocked'
    assert not result['terminal']['receipt']['verification']['checks_passed']
    assert 'verification_result_missing' in result['terminal']['summary']
    assert COMMAND in result['terminal']['summary']
    assert 'never observed' not in result['terminal']['summary']


@pytest.mark.parametrize('identity', ['old', 'missing'])
def test_refusal_does_not_make_old_or_identity_missing_verification_current(tmp_path, identity):
    from codey.operations.completion_gate import evaluate
    from codey.operations.kernel_execution import execute_turn
    from codey.runtime.core.models import ToolCall
    from tests.test_kernel_unknown_verification_invalidates_success import make_session

    session = make_session(tmp_path)
    kwargs = ({'workspace_revision': 6, 'workspace_fingerprint': 'sha256:' + 'b' * 64}
              if identity == 'old' else {})
    session.record_verification(COMMAND, 1, True, exit_code=0, **kwargs)
    execute_turn(session, [ToolCall('run', {'command': 'python -c "print(12345)"', 'path': '.'})],
                 project_path=tmp_path, run_id='stale', turn=1)
    assert len(session.verifications) == 1
    assert not evaluate(session, 'done').complete
