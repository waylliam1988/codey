"""Changes refresh retains readable results while freshness gates restoration."""
from __future__ import annotations

import pytest
from playwright.sync_api import expect

from tests.test_ui_workflow import page as page
from tests.test_ui_workflow import ui_browser as ui_browser


def changes(text='old value'):
    return {'ok': True, 'mode': 'snapshot', 'files': [{'path':'one.py','status':'M','additions':1,'deletions':1}],
            'diff': f'diff --git a/one.py b/one.py\n--- a/one.py\n+++ b/one.py\n@@ -1 +1 @@\n-before\n+{text}\n'}


def open_diff(page):
    page.route('**/api/changes', lambda r: r.fulfill(json=changes()))
    page.evaluate("CodeyChangesDrawer.open('E:/project')")
    page.locator('.change-file > button').click()


def test_pending_refresh_keeps_diff_nodes_expansion_focus_and_copy(page):
    open_diff(page)
    pending = []
    page.route('**/api/changes', lambda r: pending.append(r))
    page.locator('.diff-pre').evaluate('e=>window.oldDiff=e')
    with page.expect_request('**/api/changes'):
        page.locator('#changes-refresh').click()
    expect(page.locator('.diff-pre')).to_contain_text('old value')
    expect(page.locator('#changes-restore')).to_be_disabled()
    page.locator('.change-file > button').focus()
    pending[0].fulfill(json=changes())
    expect(page.locator('#changes-subtitle')).to_contain_text('1 file')
    expect(page.locator('.diff-pre')).to_be_visible()
    expect(page.locator('.change-file > button')).to_be_focused()
    assert page.locator('.diff-pre').evaluate('e=>e===window.oldDiff')


@pytest.mark.parametrize('failure', ['network', 'http', 'business', 'json'])
def test_failed_refresh_labels_previous_results_and_blocks_restore(page, failure):
    open_diff(page)
    def fail(route):
        if failure == 'network':
            route.abort()
        elif failure == 'json':
            route.fulfill(body='{broken', content_type='application/json')
        else:
            route.fulfill(status=503 if failure == 'http' else 200, json={'ok':False,'error':'fixture failure'})
    page.route('**/api/changes', fail)
    page.locator('#changes-refresh').click()
    expect(page.locator('#changes-subtitle')).to_contain_text('Showing previous changes')
    expect(page.locator('.diff-pre')).to_contain_text('old value')
    expect(page.locator('#changes-restore')).to_be_disabled()
    page.evaluate("window.copiedDiff='';CodeyRender.copyText=async text=>{window.copiedDiff=text;return true}")
    page.locator('#changes-copy').click()
    assert page.evaluate('window.copiedDiff') == changes()['diff']
    expect(page.locator('#changes-refresh')).to_have_text('Retry')


def test_switch_project_removes_previous_diff_and_disables_copy_while_loading(page):
    open_diff(page)
    page.evaluate("""() => {const real=fetch;window.fetch=(url,options)=>url==='/api/changes'?new Promise(()=>{}):real(url,options);CodeyChangesDrawer.open('E:/other')}""")
    expect(page.locator('#changes-body')).not_to_contain_text('old value')
    expect(page.locator('#changes-copy')).to_be_disabled()


def test_restore_success_with_failed_refresh_reports_both_facts(page):
    open_diff(page)
    page.route('**/api/changes/restore', lambda r: r.fulfill(json={'ok':True}))
    page.route('**/api/changes', lambda r: r.fulfill(status=503, json={'ok':False}))
    page.locator('#changes-restore').click()
    expect(page.locator('#changes-subtitle')).to_contain_text('Restored')
    expect(page.locator('#changes-subtitle')).to_contain_text('Showing previous changes')
    expect(page.locator('#changes-restore')).to_be_disabled()


def test_same_project_reopen_does_not_steal_diff_focus(page):
    open_diff(page)
    page.locator('.change-file > button').focus()
    page.evaluate("CodeyChangesDrawer.open('E:/project')")
    expect(page.locator('.change-file > button')).to_be_focused()


def test_growth_in_earlier_file_keeps_visible_diff_line_at_same_position(page):
    initial = changes('top\n' + '+line\n' * 80)
    second = changes('target\n' + '+following\n' * 80)['diff'].replace('one.py','two.py')
    initial['files'].append({'path':'two.py','additions':81})
    initial['diff'] += second
    page.route('**/api/changes', lambda r:r.fulfill(json=initial))
    page.evaluate("CodeyChangesDrawer.open('E:/project')")
    for button in page.locator('.change-file > button').all():
        button.click()
    page.locator('[data-path="two.py"] .diff-line').first.evaluate("e=>{e.scrollIntoView({block:'start'});window.line=e;window.lineTop=e.getBoundingClientRect().top;}")
    grown = {**initial,'diff':initial['diff'].replace('+top\n', '+top\n'+'+inserted\n'*40)}
    page.route('**/api/changes', lambda r:r.fulfill(json=grown))
    page.evaluate("CodeyChangesDrawer.open('E:/project')")
    assert page.evaluate('Math.abs(window.line.getBoundingClientRect().top-window.lineTop)') < 2
