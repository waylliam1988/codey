"""Only sustained draft failures interrupt the otherwise quiet composer."""
from __future__ import annotations

import pytest
from playwright.sync_api import expect

from tests.test_ui_workflow import page as page
from tests.test_ui_workflow import ui_browser as ui_browser


def fail_draft_saves(page):
    page.route('**/api/ui_state', lambda route: route.fulfill(status=503, json={'error': 'offline'}))
    page.locator('#task').fill('Keep this draft')


def test_transient_save_failure_is_silent_and_keeps_focus(page):
    before = page.locator('.composer-box').bounding_box()
    fail_draft_saves(page)
    assert page.evaluate('UiState.ensureSaved()') is False
    expect(page.locator('#ui-save-notice')).to_be_hidden()
    expect(page.locator('#task')).to_be_focused()
    expect(page.locator('#task')).to_have_value('Keep this draft')
    assert page.locator('.composer-box').bounding_box() == before


@pytest.mark.parametrize('width', [1280, 360, 320])
def test_sustained_failure_names_the_draft_without_moving_input(page, width):
    page.set_viewport_size({'width': width, 'height': 800})
    before = page.locator('.composer-box').bounding_box()
    fail_draft_saves(page)
    page.evaluate('async () => {for(let i=0;i<3;i++) await UiState.ensureSaved()}')
    expect(page.locator('#ui-save-notice')).to_be_visible()
    expect(page.locator('#ui-save-message')).to_have_text('Draft changes couldn’t be saved')
    assert page.locator('.composer-box').bounding_box() == before
    assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')


@pytest.mark.parametrize('move_focus', [False, True])
def test_successful_retry_retains_new_focus_or_returns_retry_focus_to_input(page, move_focus):
    fail_draft_saves(page)
    page.evaluate('async () => {for(let i=0;i<3;i++) await UiState.ensureSaved()}')
    pending = []
    page.route('**/api/ui_state', lambda route: pending.append(route))
    page.locator('#ui-save-retry').focus()
    page.keyboard.press('Enter')
    if move_focus:
        page.locator('#ctx-research').focus()
    page.keyboard.press('Enter') if not move_focus else None
    assert len(pending) == 1
    body = pending[0].request.post_data_json
    pending[0].fulfill(json={'ok': True, 'revision': body['base_revision'] + 1})
    expect(page.locator('#ui-save-notice')).to_be_hidden()
    expect(page.locator('#ctx-research' if move_focus else '#task')).to_be_focused()
    expect(page.locator('#task')).to_have_value('Keep this draft')


def test_narrow_composer_width_is_independent_of_context_text(page):
    page.set_viewport_size({'width': 320, 'height': 800})
    assert page.locator('main').bounding_box()['width'] == 320
    assert page.locator('.composer-box').bounding_box()['width'] == 288
