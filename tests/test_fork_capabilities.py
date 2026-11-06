from __future__ import annotations

import subprocess
from copy import deepcopy

import pytest

from codey.app import fork_workspaces
from codey.app.fork_workspaces import GitCommandError, capabilities, git


def test_projectless_and_plain_directory_are_unavailable_without_creating_anything(tmp_path):
    assert capabilities('')['worktree_reason'] == 'no_project'
    info = capabilities(str(tmp_path))
    assert info['worktree'] is False
    assert info['worktree_status'] == 'unavailable'
    assert info['worktree_reason'] == 'not_git'
    assert list(tmp_path.iterdir()) == []


def test_empty_repository_is_unavailable_until_it_has_a_commit(tmp_path):
    git(tmp_path, 'init')
    info = capabilities(str(tmp_path))
    assert info['worktree_status'] == 'unavailable' and info['worktree_reason'] == 'no_commits'
    assert info['worktree'] is False
    git(tmp_path, '-c', 'user.name=Test', '-c', 'user.email=test@localhost',
        'commit', '--allow-empty', '-m', 'initial')
    assert capabilities(str(tmp_path))['worktree_status'] == 'available'
    nested = tmp_path / 'nested'
    nested.mkdir()
    assert capabilities(str(nested))['worktree'] is True


@pytest.mark.parametrize('kind', ['missing', 'file'])
def test_invalid_project_directory_is_an_error(tmp_path, kind):
    path = tmp_path / 'project'
    if kind == 'file':
        path.write_text('not a directory')
    info = capabilities(str(path))
    assert info['worktree_status'] == 'error'
    assert info['worktree_reason'] == 'invalid_path'


@pytest.mark.parametrize(('error', 'reason'), [
    (FileNotFoundError('git'), 'git_missing'),
    (subprocess.TimeoutExpired('git', 5), 'timeout'),
    (PermissionError('Access denied'), 'git_error'),
    (GitCommandError('fatal: detected dubious ownership in repository', 128), 'git_error'),
    (ValueError('Unexpected Git failure'), 'git_error'),
])
def test_check_errors_are_distinct_from_unsupported_directories(tmp_path, monkeypatch, error, reason):
    def fail(*_args, **_kwargs):
        raise error

    monkeypatch.setattr(fork_workspaces, 'git', fail)
    info = capabilities(str(tmp_path))
    assert info['worktree'] is False and info['is_worktree'] is False
    assert info['worktree_status'] == 'error'
    assert info['worktree_reason'] == reason
    assert info['worktree_message']


def test_head_check_failure_is_not_silently_treated_as_an_empty_repository(tmp_path, monkeypatch):
    def fail_head(_root, *_args, **_kwargs):
        if '--verify' in _args:
            raise GitCommandError('Cannot read repository objects', 128)
        return str(tmp_path)

    monkeypatch.setattr(fork_workspaces, 'git', fail_head)
    info = capabilities(str(tmp_path))
    assert info['worktree_status'] == 'error' and info['worktree_reason'] == 'git_error'


def test_capability_route_reports_projectless_and_failed_checks_without_mutating_state(tmp_path, monkeypatch):
    from codey.app.context import AppContext
    from codey.app.conversation_routes import fork_response

    ctx = AppContext(tmp_path / 'state', start_scheduler=False)
    ctx.save_ui_state({'active_id': 'chat', 'projects': [], 'sessions': [{'id': 'chat', 'title': 'Chat'}]},
                      base_revision=0)
    query = {'session_id': ['chat'], 'capabilities': ['1']}
    before = deepcopy(ctx.load_ui_state())
    status, info = fork_response(ctx, query)
    assert status == 200 and info['worktree_reason'] == 'no_project'
    assert info['worktree_status'] == 'unavailable'
    assert ctx.load_ui_state() == before

    state = deepcopy(before)
    state['projects'] = [{'id': 'p', 'name': 'Project', 'path': str(tmp_path)}]
    state['sessions'][0]['projectId'] = 'p'
    ctx.save_ui_state(state, base_revision=state['revision'])
    before = deepcopy(ctx.load_ui_state())

    def timeout(*_args, **_kwargs):
        raise subprocess.TimeoutExpired('git', 5)

    monkeypatch.setattr(fork_workspaces, 'git', timeout)
    status, info = fork_response(ctx, query)
    assert status == 200 and info['ok'] is True
    assert info['worktree_status'] == 'error' and info['worktree_reason'] == 'timeout'
    assert ctx.load_ui_state() == before
    assert ctx.durable.tasks() == ()
