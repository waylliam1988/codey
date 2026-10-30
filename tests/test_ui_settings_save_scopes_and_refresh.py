"""Model preferences and connection settings save separately without losing refresh context."""
from __future__ import annotations

from playwright.sync_api import expect

from tests.test_ui_model_management import models as models
from tests.test_ui_workflow import model_settings_route, open_connection_settings
from tests.test_ui_workflow import page as page
from tests.test_ui_workflow import ui_browser as ui_browser


def test_model_save_tracks_semantic_changes_and_ignores_catalog_refresh(page, models):
    page.locator('#btn-settings').click()
    save = page.locator('#model-settings-save')
    expect(save).to_be_disabled()
    choice = page.get_by_role('checkbox', name='Enable Websites', exact=True)
    choice.uncheck()
    expect(save).to_be_enabled()
    choice.check()
    expect(save).to_be_disabled()
    source = page.locator('[data-source-id="partner"]')
    source.locator('summary').click()
    page.route('**/api/model_catalog', lambda r: r.fulfill(json={'ok': True, 'source': models[0]['sources'][1]}))
    source.get_by_role('button', name='Refresh models').click()
    expect(source.get_by_role('button', name='Refresh models')).to_be_enabled()
    expect(save).to_be_disabled()
    assert models[1] == []


def test_refresh_keeps_other_groups_search_staged_choices_and_moved_focus(page, models):
    source_data = models[0]['sources'][1]
    source_data['models'] += [{'id': f'extra-{i}', 'name': f'Extra {i}'} for i in range(10)]
    pending = []
    page.route('**/api/model_catalog', lambda r: pending.append(r))
    page.locator('#btn-settings').click()
    websites = page.locator('[data-source-id="websites"]')
    partner = page.locator('[data-source-id="partner"]')
    websites.locator('summary').click()
    partner.locator('summary').click()
    websites.get_by_role('checkbox', name='Use DeepSeek', exact=True).uncheck()
    search = partner.get_by_role('searchbox')
    search.fill('Chosen')
    search.evaluate('e=>window.savedSearch=e')
    with page.expect_request('**/api/model_catalog'):
        partner.get_by_role('button', name='Refresh models').click()
    search.focus()
    pending[-1].fulfill(json={'ok': True, 'source': {**source_data, 'models': [*source_data['models'], {'id':'late','name':'Late'}]}})
    expect(partner.get_by_role('button', name='Refresh models')).to_be_enabled()
    expect(search).to_have_value('Chosen')
    expect(search).to_be_focused()
    assert search.evaluate('e=>e===window.savedSearch')
    expect(websites.locator('details')).to_have_attribute('open', '')
    expect(websites.get_by_role('checkbox', name='Use DeepSeek', exact=True)).not_to_be_checked()
    assert models[1] == []


def test_connection_save_is_independent_and_returns_to_clean_state(page, models):
    local = model_settings_route(page)
    local['connected'] = True
    page.route('**/api/local_provider', lambda r: r.fulfill(json={'ok': True, 'local': local}))
    open_connection_settings(page)
    connection = page.locator('#local-config-save')
    expect(connection).to_be_disabled()

    field = page.locator('#local-base-url')
    field.fill('http://127.0.0.1:9999/v1')
    expect(connection).to_be_enabled()
    expect(page.locator('#model-settings-save')).to_be_disabled()
    field.fill(local['base_url'])
    expect(connection).to_be_disabled()
    page.locator('#local-api-key').fill('new-test-key')
    expect(connection).to_be_enabled()
    page.locator('#local-api-key').fill('')
    expect(connection).to_be_disabled()


def test_context_auto_survives_unrelated_save_and_can_restore_follow_server(page):
    local = model_settings_route(page)
    local["context_mode"] = "auto"
    saved = []

    def route(request):
        if request.request.method == "POST":
            body = request.request.post_data_json
            saved.append(body)
            local["context_mode"] = "auto" if body["context_window_tokens"] is None else "manual"
            if body["context_window_tokens"] is not None:
                local["context"] = {"context_window_tokens": int(body["context_window_tokens"])}
        request.fulfill(json={"ok": True, "local": local})

    page.route("**/api/local_provider", route)
    open_connection_settings(page)
    page.locator("#local-advanced summary").click()
    expect(page.locator("#local-context-preset-button")).to_have_text("Follow server")
    page.locator("#local-display-name").fill("My local model")
    page.locator("#local-config-save").click()
    expect(page.locator("#local-config-save")).to_be_disabled()
    assert saved[-1]["context_window_tokens"] is None
    page.locator("#local-context-preset-button").click()
    page.get_by_role("option", name="131,072 tokens", exact=True).click()
    page.locator("#local-config-save").click()
    expect(page.locator("#local-config-save")).to_be_disabled()
    assert saved[-1]["context_window_tokens"] == "131072"
    page.locator("#local-context-preset-button").click()
    page.get_by_role("option", name="Follow server", exact=True).click()
    page.locator("#local-config-save").click()
    expect(page.locator("#local-config-save")).to_be_disabled()
    assert saved[-1]["context_window_tokens"] is None


def test_catalog_removing_focused_model_returns_focus_to_its_source(page, models):
    pending = []
    page.route('**/api/model_catalog', lambda r:pending.append(r))
    page.locator('#btn-settings').click()
    source = page.locator('[data-source-id="partner"]')
    source.locator('summary').click()
    with page.expect_request('**/api/model_catalog'):
        source.get_by_role('button', name='Refresh models').click()
    source.get_by_role('checkbox', name='Use Unselected model',exact=True).focus()
    pending[-1].fulfill(json={'ok':True,'source':{**models[0]['sources'][1],'models':models[0]['sources'][1]['models'][:1]}})
    expect(source.get_by_role('button', name='Refresh models')).to_be_enabled()
    expect(source.locator('summary')).to_be_focused()
