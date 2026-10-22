"""Observable admission, exact grant scope, revocation and real exit-code contracts."""
from __future__ import annotations

import json
from dataclasses import replace
from unittest.mock import Mock

import pytest

from codey.agents.tools import AgentToolFns
from codey.app.context import AppContext
from codey.app.shell_approval_state import restore_pending_shell_approvals
from codey.operations.task_execution import ExecutionDelegate
from codey.operations.task_loop import _approval_stop
from codey.operations.task_session import TaskSession
from codey.policies.command_approval import shell_compatibility_error
from codey.policies.task_policy import TaskPolicy
from codey.runtime.core.models import ToolCall
from codey.storage.command_permissions import CommandPermissionStore, project_rule_available
from tests.support.shell_approval import seed_requested_approval


@pytest.mark.parametrize("command", ['python -c "print(1)"', 'python -m unittest 2>&1 | tail -n 40'])
def test_run_creates_approval_for_original_call_without_executing(tmp_path, command):
    requests = []
    call = ToolCall('run', {'command': command, 'path': '.', 'output_tail_lines': 40}, call_id='original-run')
    result = _approval_stop([call], project_path=tmp_path, on_shell_request=requests.append, turn=2,
                            policy=TaskPolicy(grants=frozenset({'project.verify', 'shell.approval'})))
    assert result.stop_reason == 'approval'
    assert requests[0].tool_name == 'run'
    assert requests[0].tool_args == call.args
    assert requests[0].call_id == 'original-run'


@pytest.mark.parametrize(('command', 'path', 'grants'), [
    ('python -m unittest', '.', {'project.verify', 'shell.approval'}),
    ('python -c "print(1)"', '..', {'project.verify', 'shell.approval'}),
    ('python -c "print(1)"', '.', {'project.verify'}),
    ('python -c "print(1)"', '.', {'shell.approval'}),
])
def test_allowlisted_or_unauthorized_commands_do_not_create_approval(tmp_path, command, path, grants):
    requests = []
    result = _approval_stop([ToolCall('run', {'command': command, 'path': path})],
                            project_path=tmp_path, on_shell_request=requests.append, turn=1,
                            policy=TaskPolicy(grants=frozenset(grants)))
    assert result is None and requests == []


def test_mixed_batch_never_silently_discards_prior_calls(tmp_path):
    requests = []
    result = _approval_stop([ToolCall('read_file', {'path': 'a.py'}),
                             ToolCall('run', {'command': 'python -c "print(1)"'})],
                            project_path=tmp_path, on_shell_request=requests.append, turn=1,
                            policy=TaskPolicy(grants=frozenset({'project.read', 'project.verify', 'shell.approval'})))
    assert result is None and not requests


def test_invalid_tail_never_creates_approval_and_approved_run_honors_tail(tmp_path):
    from codey.app.shell_service import shell_result_row

    requests = []
    result = _approval_stop([ToolCall('run', {'command': 'python -c "print(1)"', 'output_tail_lines': True})],
                            project_path=tmp_path, on_shell_request=requests.append, turn=1,
                            policy=TaskPolicy(grants=frozenset({'project.verify', 'shell.approval'})))
    assert result is None and not requests
    row = shell_result_row({'call_id': 'original-run', 'tool_name': 'run',
                            'command': 'python -c "print(1)"', 'tool_args': {'output_tail_lines': 2}},
                           {'output': 'a\nb\nc\nd', 'ok': False, 'exit_code': 1}, approved=True)
    assert row.outcome.exit_code == 1 and not row.outcome.ok
    assert 'Exit code: 1' in row.outcome.model_text and 'c\nd' in row.outcome.model_text
    assert 'a\nb' not in row.outcome.model_text


def test_windows_heredoc_requires_explicit_compatible_interpreter(monkeypatch):
    monkeypatch.setattr('codey.policies.command_approval.sys.platform', 'win32')
    assert 'Bash heredoc' in shell_compatibility_error("python - <<'PY'\nprint(1)\nPY")
    assert not shell_compatibility_error('bash -lc "python - <<PY\nprint(1)\nPY"')
    assert not shell_compatibility_error('python -c "print(\'<<PY\')"')


def test_exact_grants_bind_project_cwd_executor_and_chat_and_survive_restart(tmp_path):
    project = tmp_path / 'project'
    project.mkdir()
    pending = {'project': str(project), 'cwd': '.', 'command': 'git status --short', 'session_id': 'chat-a'}
    store = CommandPermissionStore(tmp_path)
    store.remember(pending, 'session')
    assert CommandPermissionStore(tmp_path).matching(pending)
    for changes in ({'command': 'git status --short && echo extra'}, {'cwd': 'other'},
                    {'session_id': 'chat-b'}, {'project': str(tmp_path / 'another')}):
        assert not store.matching({**pending, **changes})
    rule = store.list_rules(session_id='chat-a')[0]
    store.revoke(rule['id'])
    assert not store.matching(pending)
    store.remember(pending, 'project')
    assert store.matching({**pending, 'session_id': 'chat-b'})


@pytest.mark.parametrize('command', ['python -c "print(1)"', 'git status | python evil.py',
                                    'git status && echo extra', 'powershell -Command anything'])
def test_arbitrary_scripts_and_compound_commands_cannot_be_saved_as_project_rules(tmp_path, command):
    assert not project_rule_available(command)
    with pytest.raises(ValueError):
        CommandPermissionStore(tmp_path).remember({'project': str(tmp_path), 'command': command}, 'project')


def test_corrupt_saved_rules_fail_closed(tmp_path):
    store = CommandPermissionStore(tmp_path)
    store.path.write_text('{broken', encoding='utf-8')
    with pytest.raises(ValueError):
        store.matching({'command': 'echo x', 'project': str(tmp_path), 'session_id': 'a'})


def test_live_public_events_preserve_permission_scope_and_result_project(tmp_path):
    from codey.agents.shell_approval import shell_command_payload
    from codey.app.public_event_feed import PublicEventFeed

    feed = PublicEventFeed()
    request = json.loads(feed.encode({'type': 'shell_request', 'id': 'a', 'session_id': 's',
                                     'project': str(tmp_path), 'project_rule_available': True,
                                     'tool_name': 'run', **shell_command_payload('git status')}, 1).body)
    assert request['project'] == str(tmp_path) and request['project_rule_available'] is True
    assert request['tool_name'] == 'run'
    result = json.loads(feed.encode({'type': 'shell_result', 'id': 'a', 'project': str(tmp_path),
                                    'truncated': True}, 2).body)
    assert result['project'] == str(tmp_path) and result['truncated'] is True


def test_mode_without_shell_grant_explains_next_step_instead_of_waiting(tmp_path):
    session = TaskSession(policy=TaskPolicy(grants=frozenset({'project.verify'})), project=str(tmp_path))
    result, ok, _ = ExecutionDelegate(session=session, project_path=tmp_path, tool_fns=AgentToolFns()).execute(
        ToolCall('run', {'command': 'python -c "print(1)"'}),
    )
    assert not ok
    assert 'This task mode cannot request shell approval' in result.model_text
    assert 'interactive coding task' in result.model_text


def test_session_approval_is_reused_then_revocation_restores_prompt(tmp_path, monkeypatch):
    from codey.app import shell_service

    ctx = AppContext(tmp_path, start_scheduler=False)
    executed = Mock(return_value={'ok': True, 'status': 'exit', 'output': 'ok', 'exit_code': 0})
    monkeypatch.setattr(shell_service, 'execute_shell_ticket', executed)
    try:
        seed_requested_approval(ctx, 'first', continue_after=False)
        restore_pending_shell_approvals(ctx)
        code, _ = ctx.shell_approval_workflow.accept('first', True, Mock(), scope='session')
        assert code == 202 and ctx.shell_approval_workflow.wait(5)
        seed_requested_approval(ctx, 'second', continue_after=False, source_run_id='source2')
        restore_pending_shell_approvals(ctx)
        ctx.shell_approval_workflow.apply_remembered('session', 'source2')
        assert ctx.shell_approval_workflow.wait(5)
        assert ctx.shell_approval_workflow.status('second')['execution'] == 'settled'
        assert executed.call_count == 2
        rule = ctx.command_permissions.list_rules(session_id='session')[0]
        ctx.command_permissions.revoke(rule['id'])
        seed_requested_approval(ctx, 'third', continue_after=False, source_run_id='source3')
        ctx.shell_approval_workflow.apply_remembered('session', 'source3')
        assert ctx.shell_approval_workflow.status('third')['decision'] == 'pending'
        assert executed.call_count == 2
    finally:
        ctx.close()


def test_invalid_scope_or_unsafe_project_grant_does_not_decide_or_execute(tmp_path):
    ctx = AppContext(tmp_path, start_scheduler=False)
    try:
        seed_requested_approval(ctx, command='python -c "print(1)"')
        for scope in ('unknown', [], 'project'):
            code, _ = ctx.shell_approval_workflow.accept('approval', True, Mock(), scope=scope)
            assert code == 400
            assert ctx.shell_approval_workflow.status('approval')['decision'] == 'pending'
        assert not ctx.command_permissions.list_rules(session_id='session', project=str(tmp_path))
    finally:
        ctx.close()


def test_output_tail_executes_real_failing_test_and_preserves_exit_status(tmp_path):
    (tmp_path / 'test_failure.py').write_text(
        'import unittest\nclass TestFailure(unittest.TestCase):\n'
        ' def test_failure(self):\n  print("\\n".join(str(i) for i in range(80)))\n  self.fail("EXPECTED_FAILURE")\n',
        encoding='utf-8',
    )
    session = TaskSession(policy=TaskPolicy(grants=frozenset({'project.verify'})), project=str(tmp_path))
    delegate = ExecutionDelegate(session=session, project_path=tmp_path, tool_fns=AgentToolFns())
    call = ToolCall('run', {'command': 'python -m unittest discover', 'output_tail_lines': 10})
    result, ok, exit_code = delegate.execute(call)
    assert not ok and exit_code == 1
    assert result.audit['exit_code'] == 1
    assert 'Exit code: 1' in result.model_text
    assert 'Showing last 10 lines' in result.model_text
    assert 'FAILED (failures=1)' in result.model_text
    assert '\n0\n1\n2\n' not in result.model_text
    no_execution = Mock()
    delegate.tool_fns = AgentToolFns(run_command=no_execution)
    delegate.execute(replace(call, args={**call.args, 'output_tail_lines': True}))
    no_execution.assert_not_called()
