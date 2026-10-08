"""Model sources share adjacent selection actions and optional catalog refresh."""
from __future__ import annotations

import pytest
from playwright.sync_api import expect

from tests.test_ui_model_management import models as models
from tests.test_ui_workflow import page as page
from tests.test_ui_workflow import ui_browser as ui_browser


def selection_gap(source):
    select = source.get_by_role('button', name='Select all', exact=True).bounding_box()
    clear = source.get_by_role('button', name='Clear', exact=True).bounding_box()
    assert abs(select['y'] - clear['y']) < 1
    return clear['x'] - select['x'] - select['width']


@pytest.mark.parametrize('width', [1280, 360])
def test_all_sources_keep_selection_actions_adjacent_and_only_refresh_at_right(page, models, width):
    page.locator('#btn-settings').click()
    page.set_viewport_size({'width': width, 'height': 800})
    for source_id in ('websites', 'partner', 'local'):
        source = page.locator(f'.model-source[data-source-id="{source_id}"]')
        source.locator('summary').first.click()
        assert selection_gap(source) == pytest.approx(14, abs=1)
        refresh = source.get_by_role('button', name='Refresh models', exact=True)
        if source_id == 'websites':
            expect(refresh).to_have_count(0)
        else:
            expect(refresh).to_be_visible()
            toolbar = source.locator('.model-source-actions').bounding_box()
            button = refresh.bounding_box()
            clear = source.get_by_role('button', name='Clear', exact=True).bounding_box()
            assert abs(toolbar['x'] + toolbar['width'] - button['x'] - button['width']) < 1
            assert button['x'] - clear['x'] - clear['width'] >= 13


@pytest.mark.parametrize('source_id', ['websites', 'partner', 'local'])
def test_any_source_without_discovery_keeps_clear_beside_select_all(page, models, source_id):
    source_data = next(source for source in models[0]['sources'] if source['id'] == source_id)
    source_data['discoverable'] = False
    page.locator('#btn-settings').click()
    source = page.locator(f'.model-source[data-source-id="{source_id}"]')
    source.locator('summary').first.click()
    assert selection_gap(source) == pytest.approx(14, abs=1)
    expect(source.get_by_role('button', name='Refresh models', exact=True)).to_have_count(0)


def test_opening_source_groups_does_not_fetch_catalogs_or_save_preferences(page, models):
    requests = []
    page.on('request', lambda request: requests.append(request))
    page.locator('#btn-settings').click()
    expect(page.locator('.model-source')).to_have_count(3)
    for source in page.locator('.model-source').all():
        source.locator('summary').first.click()
    assert not any('/api/model_catalog' in request.url for request in requests)
    assert models[1] == []
