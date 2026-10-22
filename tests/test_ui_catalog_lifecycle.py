"""Refresh updates facts everywhere, preserving consent, drafts and history."""
from __future__ import annotations

import copy

from playwright.sync_api import expect

from tests.test_ui_model_management import models as models
from tests.test_ui_workflow import page as page
from tests.test_ui_workflow import ui_browser as ui_browser


def test_retired_selection_is_hidden_from_picker_but_explained_in_settings(page, models):
    state, posts = models
    source = state['sources'][1]
    source['models'] = [{'id': 'new-model', 'name': 'Unselected model'}]
    source['unavailable_models'] = [{'id': 'chosen-model', 'name': 'Chosen model', 'status': 'deprecated'}]
    page.evaluate('Models.applySettings', state)
    page.evaluate("""() => {
      const state = UiState.current(), s = state.sessions.find(s => s.id === 'a');
      s.provider = 'partner'; s.modelSelection = {connection_id:'partner', model:'chosen-model', name:'Chosen model'};
      UiState.apply(state); Composer.syncSession(); ProviderUI.sync('partner');
    }""")
    page.locator('#task').fill('keep draft')
    expect(page.locator('#provider-name')).to_have_text('Chosen model · Unavailable')
    expect(page.locator('#send')).to_be_disabled()
    page.locator('#provider-button').click()
    expect(page.locator('.provider-item[data-model="chosen-model"]')).to_have_count(0)
    page.keyboard.press('Escape')
    page.locator('#btn-settings').click()
    section = page.locator('.model-source[data-source-id="partner"]')
    section.locator('summary').first.click()
    expect(section.locator('.model-source-count')).to_have_text('0 selected · 1 unavailable')
    expect(section.get_by_role('checkbox', name='Use Chosen model', exact=True)).to_have_count(0)
    section.get_by_text('1 unavailable selection', exact=True).click()
    expect(section.locator('.model-source-unavailable')).to_contain_text('Chosen model · No longer available')
    expect(page.locator('#model-settings-save')).to_be_disabled()
    page.locator('#model-settings-cancel').click()
    assert posts == []
    assert page.locator('#task').input_value() == 'keep draft'
    assert page.evaluate("UiState.current().sessions.find(s => s.id === 'a').modelSelection.model") == 'chosen-model'


def test_manual_refresh_is_forced_and_updates_picker_without_saving_staged_choices(page, models):
    state, posts = models
    calls = []
    fresh = {**state['sources'][1], 'models': [{'id': 'late', 'name': 'New arrival'}],
             'unavailable_models': [{'id': 'chosen-model', 'name': 'Chosen model', 'status': 'deprecated'}]}
    page.route('**/api/model_catalog', lambda route: (
        calls.append(route.request.post_data_json), route.fulfill(json={'ok': True, 'source': fresh})
    ))
    page.locator('#btn-settings').click()
    source = page.locator('.model-source[data-source-id="partner"]')
    source.locator('summary').first.click()
    source.get_by_role('checkbox', name='Use Unselected model', exact=True).check()
    source.get_by_role('button', name='Refresh models', exact=True).click()
    expect(source.get_by_role('checkbox', name='Use New arrival', exact=True)).not_to_be_checked()
    assert calls[-1] == {'source': 'partner', 'force': True}
    expect(source.locator('.model-source-count')).to_have_text('0 selected · 2 unavailable')
    assert posts == []
    page.locator('#model-settings-cancel').click()
    page.locator('#provider-button').click()
    expect(page.locator('.provider-item[data-model="chosen-model"]')).to_have_count(0)
    assert state['preferences']['sources']['partner']['models'] == ['chosen-model']


def test_failed_refresh_reports_stale_cache_and_can_recover_without_clearing_rows(page, models):
    source_data = models[0]['sources'][1]
    failed = {**source_data, 'stale': True, 'error': 'fixture offline'}
    page.route('**/api/model_catalog', lambda route: route.fulfill(json={'ok': True, 'source': failed}))
    page.locator('#btn-settings').click()
    section = page.locator('.model-source[data-source-id="partner"]')
    section.locator('summary').first.click()
    section.get_by_role('button', name='Refresh models', exact=True).click()
    expect(section.locator('.model-source-status')).to_contain_text('Could not refresh models; using cached list')
    expect(section.get_by_role('checkbox', name='Use Chosen model', exact=True)).to_be_checked()
    expect(page.locator('#model-settings-save')).to_be_disabled()
    failed.update(stale=False, error='')
    section.get_by_role('button', name='Refresh models', exact=True).click()
    expect(section.locator('.model-source-status')).to_be_hidden()


def test_background_catalog_updates_open_settings_without_losing_search_or_choices(page, models):
    page.locator('#btn-settings').click()
    section = page.locator('.model-source[data-source-id="partner"]')
    section.locator('summary').first.click()
    section.get_by_role('checkbox', name='Use Unselected model', exact=True).check()
    # Search remains visible once it has a query even for a short list.
    section.locator('input[type="search"]').evaluate("e => { e.hidden=false; e.value='arrival'; e.dispatchEvent(new Event('input')); }")
    fresh = {**models[0]['sources'][1], 'models': [*models[0]['sources'][1]['models'], {'id': 'late', 'name': 'New arrival'}]}
    page.evaluate('Models.observeCatalog', fresh)
    expect(section.get_by_role('checkbox', name='Use New arrival', exact=True)).to_be_visible()
    expect(section.get_by_role('checkbox', name='Use New arrival', exact=True)).not_to_be_checked()
    expect(section.locator('input[type="search"]')).to_have_value('arrival')
    page.locator('#model-settings-save').click()
    assert models[1][-1]['sources']['partner']['models'] == ['chosen-model', 'new-model']


def test_older_status_reply_cannot_restore_a_retired_model(page, models):
    old = copy.deepcopy(models[0])
    old['sources'][1]['checked_at'] = 10
    fresh = {**old['sources'][1], 'models': [], 'checked_at': 20}
    page.evaluate('Models.observeCatalog', fresh)
    page.evaluate('Models.applySettings', old)
    page.evaluate('Models.observeCatalog', old['sources'][1])
    assert page.evaluate("Models.connections().find(s => s.id === 'partner').models") == []


def test_opening_settings_skips_disabled_sources_and_auto_refresh_does_not_force(page, models):
    models[0]['preferences']['sources']['local']['enabled'] = False
    calls = []
    page.route('**/api/model_catalog', lambda route: (
        calls.append(route.request.post_data_json),
        route.fulfill(json={'ok': True, 'source': models[0]['sources'][2]})
    ))
    page.locator('#btn-settings').click()
    expect(page.locator('.model-source')).to_have_count(3)
    page.locator('.model-source[data-source-id="local"] > details > summary').click()
    assert calls == []
    page.locator('.model-source[data-source-id="local"]').get_by_role('button', name='Refresh models').click()
    expect(page.locator('.model-source[data-source-id="local"] .model-source-refresh')).to_be_enabled()
    assert calls == [{'source': 'local', 'force': True}]


def test_hourly_refresh_updates_the_shared_catalog(page, models):
    calls = []
    page.clock.install()
    page.route('**/api/api_models', lambda route: (
        calls.append(route.request.url),
        route.fulfill(json={'connections': [models[0]['sources'][1]]})
    ))
    page.reload()
    page.wait_for_load_state('networkidle')
    count = len(calls)
    assert count >= 1
    models[0]['sources'][1]['models'] = []
    page.clock.fast_forward(60 * 60 * 1000 + 1)
    page.wait_for_function("Models.connections().find(s => s.id === 'partner').models.length === 0")
    assert len(calls) > count
