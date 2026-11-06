"""Message forks wait for an explicit destination in the shipped browser UI."""
from __future__ import annotations

import subprocess
from copy import deepcopy
from urllib.parse import parse_qs, urlsplit

import pytest
from playwright.sync_api import expect

from codey.app import fork_workspaces
from codey.app.conversation_routes import conversation_action_response, fork_response
from codey.app.fork_workspaces import git
from tests.test_ui_workflow import page as page
from tests.test_ui_workflow import ui_browser as ui_browser


@pytest.fixture
def fork_page(page):
    ctx = page.codey_context
    original = {session['id'] for session in ctx.load_ui_state()['sessions']}
    try:
        yield page
    finally:
        # The shared page fixture deletes its source chats on the next setup;
        # durable ancestry correctly requires deleting our children first.
        for session in ctx.load_ui_state()['sessions']:
            if session['id'] not in original and ctx.durable.has_conversation(session['id']):
                ctx.durable.delete_conversation(session['id'])


@pytest.mark.parametrize('kind', ['projectless', 'plain', 'git', 'timeout'])
def test_fork_waits_through_capability_check_and_cancel_until_explicit_choice(fork_page, tmp_path, monkeypatch, kind):
    page = fork_page
    ctx = page.codey_context
    if kind != 'projectless':
        if kind in {'git', 'timeout'}:
            git(tmp_path, 'init')
            git(tmp_path, '-c', 'user.name=Test', '-c', 'user.email=test@localhost',
                'commit', '--allow-empty', '-m', 'initial')
        state = deepcopy(ctx.load_ui_state())
        state['projects'] = [{'id': 'p', 'name': 'Project', 'path': str(tmp_path)}]
        state['sessions'][0]['projectId'] = 'p'
        state = ctx.save_ui_state(state, base_revision=state['revision'])
        page.evaluate('state => { UiState.accept(state); renderSidebar(); renderChat(); }', state)

    if kind == 'timeout':
        original_git = fork_workspaces.git

        def timeout(*_args, **_kwargs):
            raise subprocess.TimeoutExpired('git', 5)

        monkeypatch.setattr(fork_workspaces, 'git', timeout)

    ctx.durable.create_conversation('a')
    ctx.durable.append_message('a', 'user_message', {'text': 'Original question'})
    answer = ctx.durable.append_message('a', 'assistant_message', {'text': 'Original answer'})
    page.evaluate("ConversationUI.refreshHistory('a')")
    message = page.locator('.msg.asst').filter(has_text='Original answer')
    expect(message).to_be_visible()
    pending = []
    checks = []
    actions = []

    def capabilities(route):
        query = parse_qs(urlsplit(route.request.url).query)
        status, result = fork_response(ctx, query)
        if query.get('capabilities') == ['1']:
            checks.append(result)
            if len(checks) == 1:
                pending.append((route, status, result))
                return
        route.fulfill(status=status, json=result)

    def create(route):
        actions.append(route.request.post_data_json)
        status, result = conversation_action_response(ctx, actions[-1])
        route.fulfill(status=status, json=result)

    page.route('**/api/conversation_fork?*', capabilities)
    page.route('**/api/conversation_tree/action', create)
    message.hover()
    with page.expect_request('**/api/conversation_fork?*capabilities=1'):
        message.get_by_role('button', name='Fork chat from here', exact=True).click()
    dialog = page.get_by_role('dialog', name='Fork chat from here')
    options = dialog.locator('.chat-fork-destination')
    expect(dialog).to_be_visible()
    expect(options).to_have_count(2)
    expect(options.nth(0)).to_be_enabled()
    expect(options.nth(1)).to_be_disabled()
    expect(options.nth(1)).to_contain_text('Checking Git workspace')
    assert actions == []
    assert page.evaluate('activeId') == 'a'

    route, status, result = pending.pop()
    route.fulfill(status=status, json=result)
    if kind == 'git':
        expect(options.nth(1)).to_be_enabled()
    else:
        expect(options.nth(1)).to_contain_text(result['worktree_message'])
        expect(options.nth(1)).to_be_disabled()
    page.wait_for_load_state('networkidle')
    expect(dialog).to_be_visible()
    assert actions == []
    assert len(ctx.load_ui_state()['sessions']) == 2

    if kind == 'timeout':
        monkeypatch.setattr(fork_workspaces, 'git', original_git)
        dialog.get_by_role('button', name='Retry workspace check', exact=True).click()
        expect(options.nth(1)).to_be_enabled()
        assert len(checks) == 2 and actions == []

    page.keyboard.press('Escape')
    expect(dialog).not_to_be_visible()
    assert actions == []
    message.hover()
    message.get_by_role('button', name='Fork chat from here', exact=True).click()
    expect(dialog).to_be_visible()
    options.nth(0).click()
    expect(dialog).not_to_be_visible()
    page.wait_for_function("activeId.startsWith('fork_')")
    expect(page.locator('#task')).to_be_focused()
    child = page.evaluate('activeId')
    assert len(actions) == 1 and actions[0]['destination'] == 'local'
    assert actions[0]['entry_id'] == answer.entry_id
    assert len(ctx.load_ui_state()['sessions']) == 3
    assert [entry.payload['text'] for entry in ctx.durable.context(child)] == ['Original question', 'Original answer']
