"""Global chat creation stays separate from the explicit project action."""
import pytest
from playwright.sync_api import expect

from tests.test_ui_workflow import page, ui_browser  # noqa: F401 -- shared browser fixtures


@pytest.mark.parametrize('entry', ['button', 'shortcut'])
def test_global_new_chat_leaves_the_selected_project_and_project_action_keeps_it(page, entry):  # noqa: F811
    page.evaluate("""() => {
      const state = UiState.current();
      state.projects.push({id:'project',name:'Project Alpha',path:'E:/project-alpha',expanded:true});
      state.sessions.forEach(session => session.projectId = 'project');
      UiState.apply(state);
      renderSidebar(); renderChat();
    }""")
    assert page.evaluate('activeSession().projectId') == 'project'
    if entry == 'button':
        page.locator('#btn-new-chat').click()
    else:
        page.keyboard.press('Control+n')
    assert page.evaluate('activeSession().projectId') is None
    expect(page.locator('#session-list > .session-item.active')).to_have_count(1)
    expect(page.locator('.project-children .session-item')).to_have_count(2)
    page.route('**/api/run', lambda route: route.fulfill(json={'ok': True, 'run_id': 'projectless-run'}))
    page.locator('#task').fill('A general question outside any project.')
    with page.expect_request('**/api/run') as request:
        page.locator('#send').click()
    assert request.value.post_data_json.get('project') in (None, '')
    page.locator('.project-row').hover()
    page.get_by_role('button', name='New chat in Project Alpha', exact=True).click()
    assert page.evaluate('activeSession().projectId') == 'project'
    expect(page.locator('.project-children .session-item.active')).to_have_count(1)
