"""Local save feedback shares the context line without moving the composer."""
from __future__ import annotations

import pytest
from playwright.sync_api import expect

from tests.test_ui_workflow import page as page
from tests.test_ui_workflow import ui_browser as ui_browser


def composer_geometry(page):
    return page.locator('.composer-box').bounding_box()


def fail_save(page):
    page.route('**/api/ui_state', lambda route: route.fulfill(status=409, json={'revision': 9}))
    page.locator('#task').fill('Keep this draft')
    page.evaluate('CodeyUiState.flush()')
    expect(page.locator('#ui-save-notice')).to_be_visible()


@pytest.mark.parametrize('width', [1280, 360, 320])
def test_failed_save_stays_on_context_baseline_without_moving_input(page, width):
    page.set_viewport_size({'width': width, 'height': 800})
    before = composer_geometry(page)
    fail_save(page)
    expect(page.locator('#ui-save-notice')).to_contain_text('Not saved')
    assert composer_geometry(page) == before
    geometry = page.evaluate("""() => {
      const folder = document.getElementById('ctx-folder');
      const range = document.createRange(); range.selectNodeContents(folder);
      const message = document.getElementById('ui-save-message');
      const messageRange = document.createRange(); messageRange.selectNodeContents(message);
      return {folder:range.getBoundingClientRect().bottom,
        message:messageRange.getBoundingClientRect().bottom,
        research:document.getElementById('ctx-research').getBoundingClientRect().right,
        notice:document.getElementById('ui-save-notice').getBoundingClientRect().left,
        overflow:document.documentElement.scrollWidth > innerWidth};
    }""")
    assert abs(geometry['folder'] - geometry['message']) < 1
    assert geometry['research'] < geometry['notice']
    assert geometry['overflow'] is False
    assert 'Could not save local changes' in page.locator('#ui-save-notice').aria_snapshot()


def test_long_folder_truncates_without_hiding_research_or_save_retry(page):
    page.set_viewport_size({'width': 320, 'height': 800})
    page.evaluate("""() => {
      const state = CodeyUiState.current();
      state.projects.push({id:'long',name:'An extremely long project folder name '.repeat(8),path:'E:/project'});
      state.sessions.find(s=>s.id==='a').projectId='long';
      updateComposerContext();
    }""")
    fail_save(page)
    assert page.locator('#ctx-folder').evaluate('e=>e.scrollWidth > e.clientWidth')
    folder = page.locator('#ctx-folder').bounding_box()
    research = page.locator('#ctx-research').bounding_box()
    retry = page.locator('#ui-save-retry').bounding_box()
    assert folder['width'] > 0
    assert folder['x'] + folder['width'] < research['x']
    assert research['x'] + research['width'] < retry['x']
    assert retry['x'] + retry['width'] <= 320
    expect(page.locator('#ui-save-retry')).to_be_in_viewport()
    assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')


def test_retry_shows_saving_in_place_until_latest_queued_edit_is_saved(page):
    fail_save(page)
    before = composer_geometry(page)
    pending, requests = [], []

    def save(route):
        requests.append(route.request)
        pending.append(route)

    page.route('**/api/ui_state', save)
    page.locator('#ui-save-retry').click()
    expect(page.locator('#ui-save-notice')).to_contain_text('Saving…')
    expect(page.locator('#ui-save-retry')).to_be_disabled()
    assert composer_geometry(page) == before
    page.locator('#task').fill('New input while saving')
    page.evaluate('CodeyUiState.flush()')
    with page.expect_request('**/api/ui_state'):
        pending.pop(0).fulfill(json={'ok': True, 'revision': 10})
    page.wait_for_function("document.getElementById('ui-save-message').textContent==='Saving…'")
    expect(page.locator('#ui-save-notice')).to_be_visible()
    assert len(requests) == 2
    assert requests[-1].post_data_json['state']['sessions'][0]['draft']['text'] == 'New input while saving'
    pending.pop(0).fulfill(json={'ok': True, 'revision': 11})
    expect(page.locator('#ui-save-notice')).to_be_hidden()
    expect(page.locator('#task')).to_be_focused()
    expect(page.locator('#task')).to_have_value('New input while saving')
    assert composer_geometry(page) == before
    assert {request.method for request in requests} == {'POST'}
    assert page.locator('.msg.user').count() == 0


@pytest.mark.parametrize('status', [200, 500])
def test_retry_completion_does_not_take_new_focus_and_failure_restores_one_action(page, status):
    fail_save(page)
    pending = []
    page.route('**/api/ui_state', lambda route: pending.append(route))
    page.locator('#ui-save-retry').click()
    expect(page.locator('#ui-save-retry')).to_be_disabled()
    page.locator('#ctx-research').focus()
    pending.pop().fulfill(status=status, json={'ok': status == 200, 'revision': 10})
    if status == 500:
        expect(page.locator('#ui-save-notice')).to_contain_text('Not saved')
    else:
        expect(page.locator('#ui-save-notice')).to_be_hidden()
    expect(page.locator('#ui-save-retry')).to_be_enabled()
    expect(page.locator('#ctx-research')).to_be_focused()
    expect(page.locator('#ui-save-retry')).to_have_count(1)
    expect(page.locator('#task')).to_have_value('Keep this draft')


def test_older_save_success_does_not_clear_status_of_queued_unsaved_input(page):
    fail_save(page)
    pending = []
    page.route('**/api/ui_state', lambda route: pending.append(route))
    page.locator('#ui-save-retry').click()
    page.locator('#task').fill('Not yet saved')
    page.evaluate('CodeyUiState.flush()')
    with page.expect_request('**/api/ui_state'):
        pending.pop(0).fulfill(json={'ok': True, 'revision': 10})
    expect(page.locator('#ui-save-notice')).to_be_visible()
    pending.pop(0).fulfill(status=500, json={'ok': False})
    expect(page.locator('#ui-save-retry')).to_be_enabled()
    expect(page.locator('#task')).to_have_value('Not yet saved')


def test_successful_retry_returns_its_keyboard_focus_to_input(page):
    fail_save(page)
    pending = []
    page.route('**/api/ui_state', lambda route: pending.append(route))
    page.locator('#ui-save-retry').focus()
    page.keyboard.press('Enter')
    expect(page.locator('#ui-save-notice')).to_contain_text('Saving…')
    page.keyboard.press('Enter')
    assert len(pending) == 1
    pending.pop().fulfill(json={'ok': True, 'revision': 10})
    expect(page.locator('#ui-save-notice')).to_be_hidden()
    expect(page.locator('#task')).to_be_focused()


def test_save_status_stays_visible_while_new_input_waits_for_coalesced_save(page):
    fail_save(page)
    pending = []
    page.route('**/api/ui_state', lambda route: pending.append(route))
    page.locator('#ui-save-retry').click()
    page.locator('#task').fill('Waiting for the normal save timer')
    with page.expect_request('**/api/ui_state'):
        pending.pop(0).fulfill(json={'ok': True, 'revision': 10})
    expect(page.locator('#ui-save-notice')).to_contain_text('Saving…')
    pending.pop(0).fulfill(json={'ok': True, 'revision': 11})
    expect(page.locator('#ui-save-notice')).to_be_hidden()
    expect(page.locator('#task')).to_have_value('Waiting for the normal save timer')


def test_normal_background_save_is_quiet_and_keeps_focus(page):
    pending = []
    page.route('**/api/ui_state', lambda route: pending.append(route))
    page.locator('#task').fill('Ordinary input')
    page.evaluate('CodeyUiState.flush()')
    expect(page.locator('#ui-save-notice')).to_be_hidden()
    pending.pop().fulfill(json={'ok': True, 'revision': 10})
    expect(page.locator('#ui-save-notice')).to_be_hidden()
    expect(page.locator('#task')).to_be_focused()


def test_narrow_composer_uses_available_width_independently_of_context_text(page):
    page.set_viewport_size({'width': 320, 'height': 800})
    assert page.locator('main').bounding_box()['width'] == 320
    assert page.locator('.composer-box').bounding_box()['width'] == 288
