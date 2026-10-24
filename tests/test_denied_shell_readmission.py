"""A denied invocation cannot acquire another approval ID in the same task."""
from dataclasses import replace
from pathlib import Path

import pytest

from codey.durable import DurableSession, SQLiteDurableStore
from codey.durable.model import ApprovalRecord, ApprovalStatus
from codey.operations.kernel_execution import execute_turn
from codey.operations.task_loop import _approval_stop
from codey.operations.task_session import TaskSession
from codey.policies.command_line import resolve_shell
from codey.policies.shell_analysis import analyze_shell
from codey.policies.task_policy import TaskPolicy
from codey.runtime.core.models import ToolCall
from codey.storage.managed_outputs import ManagedOutputStore

COMMAND = 'echo denied-marker'


@pytest.fixture
def denied(tmp_path):
    durable = DurableSession(SQLiteDurableStore(tmp_path / 'durable.sqlite3'))
    managed = ManagedOutputStore(tmp_path)
    shell = resolve_shell()
    for task in ('original', 'new-human-request'):
        durable.admit_input(conversation_id='s', request_id=task, task_id=task,
                            text='Please run command', provider_snapshot={'provider_id': 'local'})
    artifact = managed.write_json_artifact(session_id='s', run_id='original', payload={
        'command': COMMAND, 'project': str(tmp_path), 'cwd': '.',
        'shell': shell.to_payload(), 'analysis': analyze_shell(COMMAND, shell).to_payload(),
    })
    durable.create_approval(ApprovalRecord('denied', 'original', 's', ApprovalStatus.REQUESTED,
                                         artifact.sha256, artifact.handle, '.'))
    durable.transition_approval('denied', replace(durable.approval('denied'), status=ApprovalStatus.DENIED, revision=1),
                                expected_status=ApprovalStatus.REQUESTED)
    yield durable, managed, shell, tmp_path, artifact
    durable.close()


@pytest.mark.parametrize('name', ['run', 'shell'])
def test_denied_call_does_not_reopen_approval(denied, name):
    durable, managed, shell, project, _ = denied
    cards = []
    policy = TaskPolicy(frozenset({'control', 'project.verify', 'shell.approval'}))
    result = _approval_stop([ToolCall(name, {'command': COMMAND, 'path': '.'})],
                            project_path=project, on_shell_request=cards.append, turn=2,
                            run_id='original', policy=policy, shell=shell,
                            durable=durable, managed_outputs=managed)
    assert result is None and cards == []


@pytest.mark.parametrize('restart', [False, True])
def test_denial_survives_restart_and_cannot_reach_an_executor(denied, restart):
    durable, managed, shell, project, _ = denied
    if restart:
        durable.close()
        durable = DurableSession(SQLiteDurableStore(project / 'durable.sqlite3'))
    try:
        session = TaskSession(policy=TaskPolicy(frozenset({'control', 'shell.approval'})), shell=shell)
        invoked = []
        result = execute_turn(session, [ToolCall('shell', {'command': COMMAND})], run_id='original', turn=2,
                              executors={'shell': invoked.append}, durable=durable, managed_outputs=managed)[0]
        assert not result.ok and not invoked
        assert result.audit['execution_disposition'] == 'denied_before_execution'
        assert 'new user request' in result.model_text
        assert len(durable.task('original').state['approvals']) == 1
    finally:
        if restart:
            durable.close()


def test_denial_scope_is_exact_invocation_and_original_task(denied):
    from codey.operations.shell_authorization import denied_shell_message

    durable, managed, shell, project, _ = denied
    def check(command=COMMAND, path='.', task='original'):
        return denied_shell_message(ToolCall('shell', {'command': command, 'path': path}),
                                    durable=durable, managed_outputs=managed, run_id=task,
                                    project=project, shell=shell)
    assert check()
    assert not check(command='echo different-marker')
    assert not check(path='other-directory')
    assert not check(task='new-human-request')
    assert check(path=str(Path(project).resolve()))
    assert not denied_shell_message(ToolCall('shell', {'command': COMMAND}), durable=durable,
                                    managed_outputs=managed, run_id='original', project=project,
                                    shell=replace(shell, version='different-version'))


@pytest.mark.parametrize('change', ['command', 'cwd', 'human-task'])
def test_unrelated_invocation_still_uses_existing_approval_workflow(denied, change):
    durable, managed, shell, project, _ = denied
    cards = []
    args = {'command': 'echo other-marker' if change == 'command' else COMMAND,
            'path': 'other-directory' if change == 'cwd' else '.'}
    if change == 'cwd':
        (project / 'other-directory').mkdir()
    result = _approval_stop([ToolCall('shell', args)], project_path=project,
                            on_shell_request=cards.append, turn=2,
                            run_id='new-human-request' if change == 'human-task' else 'original',
                            policy=TaskPolicy(frozenset({'control', 'shell.approval'})), shell=shell,
                            durable=durable, managed_outputs=managed)
    assert result is not None and result.stop_reason == 'approval'
    assert len(cards) == 1


def test_missing_denial_artifact_cannot_authorize_a_retry(denied, monkeypatch):
    from codey.operations.shell_authorization import denied_shell_message

    durable, managed, shell, project, _ = denied
    def missing(*args):
        raise OSError('missing approval artifact')
    monkeypatch.setattr(managed, 'read_json_artifact', missing)
    message = denied_shell_message(ToolCall('shell', {'command': COMMAND}), durable=durable,
                                  managed_outputs=managed, run_id='original', project=project, shell=shell)
    assert 'cannot verify' in message.lower()
