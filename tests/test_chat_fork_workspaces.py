from __future__ import annotations

import hashlib
import subprocess
import threading
import time
from copy import deepcopy
from pathlib import Path

import pytest

from codey.app.context import AppContext
from codey.app.conversation_routes import conversation_action_response, fork_response, retry_fork_response
from codey.app.fork_workspaces import ensure_workspace_ready, git, service


@pytest.fixture
def source(tmp_path):
    root = tmp_path / 'project'
    root.mkdir()
    git(root, 'init')
    git(root, '-c', 'user.name=Test', '-c', 'user.email=test@localhost', 'commit', '--allow-empty', '-m', 'initial')
    (root / 'tracked.txt').write_text('committed\n')
    (root / '.gitignore').write_text('ignored.txt\n')
    git(root, 'add', '.')
    git(root, '-c', 'user.name=Test', '-c', 'user.email=test@localhost', 'commit', '-m', 'files')
    ctx = AppContext(tmp_path / 'state', start_scheduler=False)
    ctx.save_ui_state({'active_id': 'chat', 'projects': [{'id': 'p', 'name': 'Project', 'path': str(root)}],
                       'sessions': [{'id': 'chat', 'title': 'Discussion', 'projectId': 'p'}]}, base_revision=0)
    ctx.durable.create_conversation('chat')
    question = ctx.durable.append_message('chat', 'user_message', {'text': 'First question'})
    answer = ctx.durable.append_message('chat', 'assistant_message', {'text': 'First answer'})
    ctx.durable.append_message('chat', 'user_message', {'text': 'Later question'})
    yield ctx, root, question.entry_id, answer.entry_id
    deadline = time.monotonic() + 10
    while not ctx.close() and time.monotonic() < deadline:
        time.sleep(.05)


def request(ctx, entry, *, destination='local', identity='request'):
    return conversation_action_response(ctx, {'action': 'fork', 'session_id': 'chat', 'entry_id': entry,
                                               'request_id': identity, 'boundary': 'turn', 'destination': destination})


def settled(ctx, child):
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        _, result = fork_response(ctx, {'session_id': [child]})
        if result['job']['status'] != 'pending':
            return result
        time.sleep(.02)
    raise AssertionError('Worktree setup did not settle')


def test_message_fork_keeps_whole_turn_and_origin_without_running_it(source):
    ctx, _, question, answer = source
    status, result = request(ctx, question)
    assert status == 200
    child = result['session_id']
    assert [e.payload['text'] for e in ctx.durable.context(child)] == ['First question', 'First answer']
    _, metadata = fork_response(ctx, {'session_id': [child]})
    assert metadata['origin'] == {'session_id': 'chat', 'entry_id': answer}
    assert ctx.durable.tasks() == ()
    _, retry = request(ctx, question)
    assert retry['session_id'] == child
    assert len(ctx.load_ui_state()['sessions']) == 2
    assert ctx.load_ui_state()['sessions'][0]['title'] == 'Discussion'


def test_worktree_captures_edits_and_untracked_without_touching_source_index(source):
    ctx, root, question, _ = source
    (root / 'tracked.txt').write_text('staged\n')
    git(root, 'add', 'tracked.txt')
    (root / 'tracked.txt').write_text('current edit\n')
    (root / 'untracked.txt').write_text('new file\n')
    (root / 'ignored.txt').write_text('not included\n')
    head, index = git(root, 'rev-parse', 'HEAD'), git(root, 'diff', '--cached')
    before = git(root, 'status', '--porcelain')
    status, response = request(ctx, question, destination='worktree')
    assert status == 202 and response['state']
    child = response['session_id']
    result = settled(ctx, child)
    assert result['job']['status'] == 'ready', result
    workspace = Path(result['job']['project_path'])
    assert workspace != root and (workspace / 'tracked.txt').read_text() == 'current edit\n'
    assert (workspace / 'untracked.txt').read_text() == 'new file\n'
    assert not (workspace / 'ignored.txt').exists()
    assert git(root, 'rev-parse', 'HEAD') == head
    assert git(root, 'diff', '--cached') == index
    assert git(root, 'status', '--porcelain') == before
    entry = next(s for s in result['state']['sessions'] if s['id'] == child)
    project = next(p for p in result['state']['projects'] if p['id'] == entry['projectId'])
    assert Path(project['path']) == workspace
    assert len(ctx.durable.context(child)) == 2
    from codey.app.documents import document_response

    (workspace / 'tracked.txt').write_bytes(b'isolated update\n')
    code, document = document_response(ctx, {'session_id': child, 'kind': 'workspace', 'path': 'tracked.txt'})
    assert code == 200 and document['content'] == 'isolated update\n'
    assert (root / 'tracked.txt').read_text() == 'current edit\n'
    _, retry = request(ctx, question, destination='worktree')
    assert retry['session_id'] == child and len(ctx.load_ui_state()['projects']) == 2
    ensure_workspace_ready(ctx, child)
    assert request(ctx, question, destination='local')[0] == 409


def test_failure_is_retryable_without_duplicate_chat_and_pending_input_is_blocked(source, monkeypatch):
    ctx, _, question, _ = source
    from codey.app import fork_workspaces

    original = fork_workspaces.git
    hold = threading.Event()

    def fail_add(root, *args, **kwargs):
        if args[:2] == ('worktree', 'add'):
            hold.wait(5)
            raise ValueError('Worktree setup test failure')
        return original(root, *args, **kwargs)

    monkeypatch.setattr(fork_workspaces, 'git', fail_add)
    _, result = request(ctx, question, destination='worktree')
    child = result['session_id']
    with pytest.raises(ValueError, match='not ready'):
        ensure_workspace_ready(ctx, child)
    from codey.app.input_queue_routes import input_action_response
    from codey.app.server import _run_submit_route

    code, blocked = input_action_response(ctx, {'action': 'create', 'id': 'blocked',
                                               'request': {'session_id': child, 'task': 'Must not run'}})
    assert code == 409 and 'not ready' in blocked['error']
    assert _run_submit_route(ctx, {'session_id': child})[0] == 409
    hold.set()
    failed = settled(ctx, child)
    assert failed['job']['status'] == 'error'
    monkeypatch.setattr(fork_workspaces, 'git', original)
    status, retry = retry_fork_response(ctx, {'session_id': child})
    assert status == 202 and retry['session_id'] == child
    assert settled(ctx, child)['job']['status'] == 'ready'
    assert len(ctx.load_ui_state()['sessions']) == 2
    assert len(ctx.load_ui_state()['projects']) == 2


def test_no_git_directory_has_only_current_workspace_and_rejects_worktree(source):
    ctx, root, question, _ = source
    directory = root.parent / 'plain'
    directory.mkdir()
    state = deepcopy(ctx.load_ui_state())
    state['projects'][0]['path'] = str(directory)
    ctx.save_ui_state(state, base_revision=state['revision'])
    _, info = fork_response(ctx, {'session_id': ['chat'], 'capabilities': ['1']})
    assert info['worktree'] is False
    status, _ = request(ctx, question, destination='worktree')
    assert status == 409


def test_capabilities_detect_existing_worktree_and_request_cannot_change_destination(source):
    ctx, root, question, _ = source
    assert request(ctx, question)[0] == 200
    assert request(ctx, question, destination='worktree')[0] == 409
    separate = root.parent / 'separate'
    git(root, 'worktree', 'add', '--detach', str(separate), 'HEAD')
    from codey.app.fork_workspaces import capabilities

    assert capabilities(str(separate)) == {'worktree': True, 'is_worktree': True}
    assert subprocess.run(['git', '-C', str(root), 'status', '--porcelain'], capture_output=True).returncode == 0


def test_recover_interrupted_pending_job_as_explicit_retry(source):
    ctx, _, question, _ = source
    _, response = request(ctx, question, destination='worktree')
    child = response['session_id']
    assert settled(ctx, child)['job']['status'] == 'ready'

    manager = service(ctx)
    from codey.storage.local_store import read_json_strict, write_json_atomic

    path = manager.home / f'{child}.json'
    record = read_json_strict(path)
    record['status'] = 'pending'
    write_json_atomic(path, record)
    recovered = manager.status(child)
    assert recovered['status'] == 'error' and 'interrupted' in recovered['error']
    assert retry_fork_response(ctx, {'session_id': child})[0] == 202
    assert settled(ctx, child)['job']['status'] == 'ready'


def test_unrelated_destination_is_not_overwritten_or_removed(source):
    ctx, _, question, _ = source
    identity = hashlib.sha256(f'{len("chat")}:chat{question}:request'.encode()).hexdigest()[:32]
    target = Path(ctx.state_home) / 'chat-forks' / f'fork_{identity}'
    target.mkdir(parents=True)
    sentinel = target / 'keep.txt'
    sentinel.write_text('User data')
    _, response = request(ctx, question, destination='worktree')
    result = settled(ctx, response['session_id'])
    assert result['job']['status'] == 'error' and 'occupied' in result['job']['error']
    assert sentinel.read_text() == 'User data'


@pytest.mark.parametrize('destination,boundary', [([], 'turn'), ('local', {})])
def test_invalid_fork_mode_is_rejected_without_mutating_history(source, destination, boundary):
    ctx, _, question, _ = source
    head = ctx.durable.head('chat')
    code, _ = conversation_action_response(ctx, {'action': 'fork', 'session_id': 'chat', 'entry_id': question,
                                                 'request_id': 'invalid', 'destination': destination, 'boundary': boundary})
    assert code == 409
    assert ctx.durable.head('chat') == head and len(ctx.load_ui_state()['sessions']) == 1
