from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from codey.app import fork_workspaces
from codey.app.context import AppContext
from codey.app.conversation_routes import (
    conversation_action_response,
    conversation_tree_response,
    fork_response,
    retry_fork_response,
)
from codey.types.json_value import require_json_integer, require_json_string
from codey.utils.object_fields import require_object_rows


@pytest.fixture()
def ctx(tmp_path: Path) -> Iterator[AppContext]:
    ctx = AppContext(tmp_path, start_scheduler=False)
    ctx.save_ui_state({'active_id': 'chat', 'sessions': [{'id': 'chat', 'title': 'Original', 'provider': 'local'}], 'projects': []}, base_revision=0)
    ctx.durable.create_conversation('chat')
    yield ctx
    ctx.close()


def test_branch_switch_is_revision_fenced_and_preserves_the_other_path(ctx: AppContext) -> None:
    first = ctx.durable.append_message('chat', 'user_message', {'text': 'one'})
    old = ctx.durable.append_message('chat', 'assistant_message', {'text': 'old answer'})
    revision = ctx.durable.head('chat')[1]
    status, _ = conversation_action_response(ctx, {'action': 'select', 'session_id': 'chat', 'entry_id': first.entry_id, 'revision': revision - 1})
    assert status == 409
    status, _ = conversation_action_response(ctx, {'action': 'select', 'session_id': 'chat', 'entry_id': first.entry_id, 'revision': revision})
    assert status == 200
    new = ctx.durable.append_message('chat', 'assistant_message', {'text': 'new answer'})
    status, tree = conversation_tree_response(ctx, {'session_id': ['chat']})
    assert status == 200
    assert {require_json_string(branch['entry_id'], 'entry ID')
            for branch in require_object_rows(tree['branches'])} == {old.entry_id, new.entry_id}
    _, preview = conversation_tree_response(ctx, {'session_id': ['chat'], 'entry_id': [old.entry_id], 'leaf_id': [old.entry_id]})
    assert [require_json_string(item['text'], 'history text')
            for item in require_object_rows(preview['items'])] == ['one', 'old answer']
    assert ctx.durable.head('chat')[0] == new.entry_id


def test_switch_rejects_a_running_conversation_and_wrong_scope(ctx: AppContext) -> None:
    entry = ctx.durable.append_message('chat', 'user_message', {'text': 'one'})
    ctx.durable.admit_input(conversation_id='chat', request_id='one', task_id='one', text='run', provider_snapshot={})
    status, _ = conversation_action_response(ctx, {'action': 'select', 'session_id': 'chat', 'entry_id': entry.entry_id, 'revision': ctx.durable.head('chat')[1]})
    assert status == 409
    ctx.durable.create_conversation('other')
    other = ctx.durable.append_message('other', 'user_message', {'text': 'private'})
    status, _ = conversation_tree_response(ctx, {'session_id': ['chat'], 'entry_id': [other.entry_id]})
    assert status == 404


def test_fork_retries_register_one_recoverable_sidebar_session(ctx: AppContext) -> None:
    entry = ctx.durable.append_message('chat', 'user_message', {'text': 'start'})
    body = {'action': 'fork', 'session_id': 'chat', 'entry_id': entry.entry_id, 'request_id': 'same'}
    status, first = conversation_action_response(ctx, body)
    assert status == 200
    _, second = conversation_action_response(ctx, body)
    assert second['session_id'] == first['session_id']
    assert len(require_object_rows(ctx.load_ui_state()['sessions'])) == 2
    child = require_json_string(first['session_id'], 'session ID')
    assert [entry.payload['text'] for entry in ctx.durable.context(child)] == ['start']
    _, tree = conversation_tree_response(ctx, {'session_id': ['chat']})
    assert child in {require_json_string(item['session_id'], 'session ID')
                     for item in require_object_rows(tree['related_sessions'])}


def test_search_window_is_bounded_and_does_not_move_the_execution_head(ctx: AppContext) -> None:
    entries = [ctx.durable.append_message('chat', 'user_message', {'text': str(index)}) for index in range(240)]
    head = ctx.durable.head('chat')
    status, page = conversation_tree_response(ctx, {'session_id': ['chat'], 'entry_id': [entries[90].entry_id]})
    assert status == 200
    items = require_object_rows(page['items'])
    assert len(items) == 100
    assert entries[90].entry_id in {require_json_string(item['id'], 'entry ID') for item in items}
    assert page['has_more'] and page['has_after']
    assert ctx.durable.head('chat') == head


def test_conversation_fork_rejects_an_unanswered_native_tool_call(ctx: AppContext) -> None:
    entry = ctx.durable.append_message('chat', 'tool_call', {'tool_calls': [{'id': 'call_one', 'function': {'name': 'read_file', 'arguments': '{}'}}]})
    status, data = conversation_action_response(ctx, {'action': 'fork', 'session_id': 'chat', 'entry_id': entry.entry_id, 'request_id': 'pending'})
    assert status == 409
    assert 'unfinished tool calls' in require_json_string(data['error'], 'error')
    assert len(require_object_rows(ctx.load_ui_state()['sessions'])) == 1


@pytest.mark.parametrize('overrides', [
    {'session_id': 42}, {'entry_id': ''}, {'revision': True}, {'revision': '1'}, {'action': 'unknown'},
])
def test_invalid_branch_commands_preserve_the_execution_head(
    ctx: AppContext, overrides: dict[str, object],
) -> None:
    entry = ctx.durable.append_message('chat', 'user_message', {'text': 'original'})
    head = ctx.durable.head('chat')
    body: dict[str, object] = {'action': 'select', 'session_id': 'chat', 'entry_id': entry.entry_id, 'revision': head[1]}
    body.update(overrides)
    status, result = conversation_action_response(ctx, body)
    assert status == 400 and result['ok'] is False
    assert ctx.durable.head('chat') == head


@dataclass
class _ForkJobs:
    records: dict[str, dict[str, object]] = field(default_factory=dict)
    calls: list[tuple[str, str, str, str]] = field(default_factory=list)

    def status(self, child: str) -> dict[str, object] | None:
        return self.records.get(child)

    def start(self, source: str, child: str, entry: str, workspace: str) -> dict[str, object]:
        self.calls.append((source, child, entry, workspace))
        job: dict[str, object] = {'source': source, 'entry': entry, 'workspace': workspace, 'status': 'pending'}
        self.records[child] = job
        return dict(job)


def test_workspace_job_routes_forward_metadata_without_creating_a_worktree(
    ctx: AppContext, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    jobs = _ForkJobs()

    def backend(owner: AppContext) -> _ForkJobs:
        assert owner is ctx
        return jobs

    monkeypatch.setattr(fork_workspaces, 'service', backend)
    state = ctx.load_ui_state()
    sessions = require_object_rows(state['sessions'])
    sessions[0]['projectId'] = 'project'
    state['sessions'] = sessions
    state['projects'] = [{'id': 'project', 'name': 'Project', 'path': str(tmp_path)}]
    ctx.save_ui_state(state, base_revision=require_json_integer(state['revision'], 'UI revision'))
    entry = ctx.durable.append_message('chat', 'user_message', {'text': 'branch'})
    status, result = conversation_action_response(ctx, {
        'action': 'fork', 'session_id': 'chat', 'entry_id': entry.entry_id,
        'request_id': 'job', 'destination': 'worktree',
    })
    assert status == 202
    child = require_json_string(result['session_id'], 'session ID')
    assert ctx.durable.has_conversation(child)
    expected = ('chat', child, entry.entry_id, str(tmp_path))
    assert jobs.calls == [expected]
    status, queried = fork_response(ctx, {'session_id': [child]})
    assert status == 200 and queried['job'] == jobs.records[child]
    status, retried = retry_fork_response(ctx, {'session_id': child})
    assert status == 202 and retried['session_id'] == child
    assert jobs.calls == [expected, expected]


@pytest.mark.parametrize('job', [None, {'source': 42, 'entry': 'entry', 'workspace': 'project'}])
def test_workspace_retry_rejects_unavailable_or_invalid_job_metadata_before_dispatch(
    ctx: AppContext, monkeypatch: pytest.MonkeyPatch, job: dict[str, object] | None,
) -> None:
    child = 'fork_' + 'a' * 32
    jobs = _ForkJobs(records={child: job} if job is not None else {})

    def backend(owner: AppContext) -> _ForkJobs:
        assert owner is ctx
        return jobs

    monkeypatch.setattr(fork_workspaces, 'service', backend)
    status, result = retry_fork_response(ctx, {'session_id': child})
    assert status == 409 and result['ok'] is False
    assert not jobs.calls
