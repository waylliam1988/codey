from __future__ import annotations

import pytest

from codey.app.context import AppContext
from codey.app.conversation_routes import conversation_action_response, conversation_tree_response


@pytest.fixture
def ctx(tmp_path):
    ctx = AppContext(tmp_path, start_scheduler=False)
    ctx.save_ui_state({'active_id': 'chat', 'sessions': [{'id': 'chat', 'title': 'Original', 'provider': 'local'}], 'projects': []}, base_revision=0)
    ctx.durable.create_conversation('chat')
    yield ctx
    ctx.close()


def test_branch_switch_is_revision_fenced_and_preserves_the_other_path(ctx):
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
    assert {branch['entry_id'] for branch in tree['branches']} == {old.entry_id, new.entry_id}
    _, preview = conversation_tree_response(ctx, {'session_id': ['chat'], 'entry_id': [old.entry_id], 'leaf_id': [old.entry_id]})
    assert [item['text'] for item in preview['items']] == ['one', 'old answer']
    assert ctx.durable.head('chat')[0] == new.entry_id


def test_switch_rejects_a_running_conversation_and_wrong_scope(ctx):
    entry = ctx.durable.append_message('chat', 'user_message', {'text': 'one'})
    ctx.durable.admit_input(conversation_id='chat', request_id='one', task_id='one', text='run', provider_snapshot={})
    status, _ = conversation_action_response(ctx, {'action': 'select', 'session_id': 'chat', 'entry_id': entry.entry_id, 'revision': ctx.durable.head('chat')[1]})
    assert status == 409
    ctx.durable.create_conversation('other')
    other = ctx.durable.append_message('other', 'user_message', {'text': 'private'})
    status, _ = conversation_tree_response(ctx, {'session_id': ['chat'], 'entry_id': [other.entry_id]})
    assert status == 404


def test_fork_retries_register_one_recoverable_sidebar_session(ctx):
    entry = ctx.durable.append_message('chat', 'user_message', {'text': 'start'})
    body = {'action': 'fork', 'session_id': 'chat', 'entry_id': entry.entry_id, 'request_id': 'same'}
    status, first = conversation_action_response(ctx, body)
    assert status == 200
    _, second = conversation_action_response(ctx, body)
    assert second['session_id'] == first['session_id']
    assert len(ctx.load_ui_state()['sessions']) == 2
    assert [entry.payload['text'] for entry in ctx.durable.context(first['session_id'])] == ['start']
    _, tree = conversation_tree_response(ctx, {'session_id': ['chat']})
    assert first['session_id'] in {item['session_id'] for item in tree['related_sessions']}


def test_search_window_is_bounded_and_does_not_move_the_execution_head(ctx):
    entries = [ctx.durable.append_message('chat', 'user_message', {'text': str(index)}) for index in range(240)]
    head = ctx.durable.head('chat')
    status, page = conversation_tree_response(ctx, {'session_id': ['chat'], 'entry_id': [entries[90].entry_id]})
    assert status == 200
    assert len(page['items']) == 100
    assert entries[90].entry_id in {item['id'] for item in page['items']}
    assert page['has_more'] and page['has_after']
    assert ctx.durable.head('chat') == head


def test_conversation_fork_rejects_an_unanswered_native_tool_call(ctx):
    entry = ctx.durable.append_message('chat', 'tool_call', {'tool_calls': [{'id': 'call_one', 'function': {'name': 'read_file', 'arguments': '{}'}}]})
    status, data = conversation_action_response(ctx, {'action': 'fork', 'session_id': 'chat', 'entry_id': entry.entry_id, 'request_id': 'pending'})
    assert status == 409
    assert 'unfinished tool calls' in data['error']
    assert len(ctx.load_ui_state()['sessions']) == 1
