"""Chat-owned drafts survive durable restore without consuming newer input."""
from __future__ import annotations

from urllib.parse import parse_qs, urlsplit

import pytest
from playwright.sync_api import expect

from codey.app.http_plumbing import resolve_web_asset
from codey.storage.ui_state_store import UiStateStore
from tests.test_ui_workflow import model_management_payload
from tests.test_ui_workflow import page as page
from tests.test_ui_workflow import ui_browser as ui_browser


@pytest.fixture
def durable_drafts(page, tmp_path):
    store = UiStateStore(tmp_path)
    store.save(page.evaluate("UiState.current()"), base_revision=0)
    requests = []

    def state_api(route):
        if route.request.method == "POST":
            payload = route.request.post_data_json
            requests.append(payload)
            saved = store.save(payload["state"], base_revision=payload["base_revision"])
            route.fulfill(json={"ok": True, "revision": saved["revision"]})
        else:
            route.fulfill(json={"state": UiStateStore(tmp_path).load()})

    page.route("**/api/ui_state", state_api)
    page.evaluate("UiState.restoreFromServer()")
    return store, requests


def test_draft_text_and_backward_utf16_selection_survive_backend_restore(page, durable_drafts):
    text = "  中文😀\n    print('草稿')\n\n"
    page.locator("#task").fill(text)
    page.locator("#task").evaluate("e => {e.setSelectionRange(2, 7, 'backward'); e.dispatchEvent(new Event('select'));}")
    with page.expect_response(lambda r: r.url.endswith('/api/ui_state') and r.request.method == 'POST'):
        page.evaluate("UiState.flush()")
    page.evaluate("localStorage.clear()")
    page.reload()
    expect(page.locator("#task")).to_have_value(text)
    assert page.locator("#task").evaluate("e => [e.selectionStart,e.selectionEnd,e.selectionDirection]") == [2, 7, "backward"]
    assert UiStateStore(durable_drafts[0].path.parent).load()["sessions"][0]["draft"]["text"] == text


def test_each_chat_restores_its_own_draft_and_clear_history_keeps_input(page, durable_drafts):
    page.locator("#task").fill("Alpha draft")
    page.evaluate("switchSession('b')")
    page.locator("#task").fill("Beta draft")
    page.evaluate("switchSession('a')")
    expect(page.locator("#task")).to_have_value("Alpha draft")
    page.evaluate("clearMessages('a')")
    expect(page.locator("#task")).to_have_value("Alpha draft")
    assert page.evaluate("UiState.current().sessions.find(s=>s.id==='b').draft.text") == "Beta draft"


@pytest.mark.parametrize('deleted', [False, True])
def test_late_acceptance_cannot_clear_edited_back_text_or_resurrect_deleted_chat(page, deleted):
    pending = []
    page.route('**/api/run', lambda route: pending.append(route))
    page.locator("#task").fill("original")
    with page.expect_request('**/api/run'):
        page.locator('#send').click()
    if deleted:
        page.evaluate("deleteSession('a')")
    else:
        page.locator("#task").fill("new input")
        page.locator("#task").fill("original")
    pending[0].fulfill(json={'ok':True,'run_id':pending[0].request.post_data_json['run_id'],'session_id':'a'})
    page.wait_for_function('!Composer.isSending()')
    if deleted:
        assert page.evaluate("UiState.current().sessions.some(s=>s.id==='a')") is False
        expect(page.locator('#task')).to_have_value('')
    else:
        expect(page.locator("#task")).to_have_value("original")


def test_accepted_draft_clear_is_durable_and_revision_never_rewinds(page, durable_drafts):
    page.locator("#task").fill("sent")
    revision = page.evaluate("UiState.current().sessions[0].draft.revision")
    page.route('**/api/run', lambda route:route.fulfill(json={'ok':True,'run_id':route.request.post_data_json['run_id'],'session_id':'a'}))
    page.locator('#send').click()
    expect(page.locator("#task")).to_have_value("")
    assert page.evaluate("UiState.current().sessions[0].draft.revision") > revision
    page.wait_for_function("UiState.current().revision > 0")
    page.reload()
    expect(page.locator("#task")).to_have_value("")


def test_send_admission_does_not_wait_for_slow_state_reconciliation(page):
    pending_state = []
    page.route("**/api/state", lambda route: pending_state.append(route))
    page.route(
        "**/api/run",
        lambda route: route.fulfill(
            json={"ok": True, "run_id": route.request.post_data_json["run_id"], "session_id": "a"}
        ),
    )
    page.locator("#task").fill("send while state is slow")
    expect(page.locator("#send")).to_be_enabled()
    with page.expect_request("**/api/run", timeout=1000):
        page.locator("#send").click()


@pytest.mark.parametrize("status", [500, 409])
def test_save_failure_recovers_without_reloading_or_running(page, status):
    calls = []
    def save(route):
        if route.request.method == 'GET':
            route.fulfill(json={'state':page.codey_context.load_ui_state()})
            return
        calls.append(route.request.method)
        base = route.request.post_data_json['base_revision']
        route.fulfill(status=status if len(calls)==1 else 200,
                      json={'ok':len(calls)>1,'revision':base+1})
    page.route("**/api/ui_state", save)
    page.locator("#task").fill("must stay")
    page.evaluate("UiState.ensureSaved()")
    assert page.evaluate("UiState.ensureSaved()") is True
    expect(page.locator("#task")).to_have_value("must stay")
    expect(page.locator("#ui-save-notice")).to_be_hidden()
    assert page.locator('.msg.user').count() == 0
    assert set(calls) == {"POST"}


def test_store_preserves_exact_long_draft_and_clamps_utf16_selection(tmp_path):
    store = UiStateStore(tmp_path)
    text = "😀" + " x\n" * 70000
    saved = store.save({"sessions": [{"id": "a", "draft": {
        "text": text, "start": 999999, "end": 999999, "direction": "backward", "revision": 12,
    }}]}, base_revision=0)
    draft = saved["sessions"][0]["draft"]
    assert draft == {"text": text, "start": len(text) + 1, "end": len(text) + 1, "direction": "backward", "revision": 12}


def test_restoring_an_empty_caret_does_not_create_a_user_edit(page):
    before = page.evaluate('UiState.current().revision')
    page.locator('#task').evaluate("e=>e.dispatchEvent(new Event('select'))")
    assert page.evaluate('UiState.current().revision') == before


def test_new_browser_context_restores_draft_from_real_http_storage(page, ui_browser):
    browser, launch = ui_browser
    token = parse_qs(urlsplit(launch).fragment)['codey_bootstrap'][0]
    response = page.context.request.post(page.url + 'api/operator_session', data={'token':token})
    assert response.ok
    page.route('**/api/ui_state', lambda route: route.continue_())
    page.locator('#task').fill('Durable 中文😀\n    exact whitespace\n')
    with page.expect_response(lambda r: r.url.endswith('/api/ui_state') and r.request.method == 'POST') as saved:
        page.evaluate('UiState.flush()')
    assert saved.value.ok
    # Only the authorization cookie crosses contexts; no localStorage cache.
    context = browser.new_context(storage_state={'cookies':page.context.cookies(),'origins':[]})
    try:
        fresh = context.new_page()
        fresh.add_init_script('window.EventSource=class {static OPEN=1;readyState=1;close(){}}')
        def asset(route):
            path, content_type = resolve_web_asset(urlsplit(route.request.url).path)
            route.fulfill(body=path.read_bytes(), content_type=content_type)
        fresh.route('**/assets/**', asset)
        def api(route):
            path = urlsplit(route.request.url).path
            if path in {'/api/ui_state','/api/operator_session'}:
                route.continue_()
            elif path == '/api/model_settings':
                route.fulfill(json=model_management_payload())
            elif path in {'/api/provider_catalog','/api/providers'}:
                route.fulfill(json={'default':'deepseek','providers':[{'id':'deepseek','label':'DeepSeek'}]})
            else:
                route.fulfill(json={'ok':True})
        fresh.route('**/api/**', api)
        fresh.goto(page.url)
        expect(fresh.locator('#task')).to_have_value('Durable 中文😀\n    exact whitespace\n')
    finally:
        context.close()


def test_transient_malformed_save_response_retains_draft_without_immediate_warning(page):
    page.route('**/api/ui_state', lambda r:r.fulfill(body='{broken',content_type='application/json'))
    page.locator('#task').fill('Still unsaved')
    assert page.evaluate('UiState.ensureSaved()') is False
    expect(page.locator('#ui-save-notice')).to_be_hidden()
    expect(page.locator('#task')).to_have_value('Still unsaved')


def test_pagehide_during_save_caches_draft_without_duplicate_beacon(page):
    pending = []
    page.route('**/api/ui_state', lambda route: pending.append(route))
    page.locator('#task').fill('Keep this local draft')
    page.evaluate('UiState.flush()')
    sent = page.evaluate("""() => {
      const beacons=[];navigator.sendBeacon=(url,body)=>{beacons.push(url);return true;};
      dispatchEvent(new PageTransitionEvent('pagehide'));
      return beacons;
    }""")
    assert sent == []
    assert 'Keep this local draft' in page.evaluate("localStorage.getItem('codey:ui-cache')")
    pending[0].fulfill(status=503,json={'error':'offline'})


def test_cached_draft_cannot_send_before_initial_server_restore_finishes(page):
    page.locator('#task').fill('Cached input')
    page.evaluate('UiState.cache()')
    state = page.evaluate('UiState.current()')
    pending = []
    def api(route):
        if route.request.method == 'GET':
            pending.append(route)
        else:
            route.fulfill(json={'ok':True,'revision':state['revision']})
    page.route('**/api/ui_state', api)
    page.reload(wait_until='domcontentloaded')
    page.wait_for_function("document.getElementById('task').value==='Cached input'")
    expect(page.locator('#task')).to_be_disabled()
    expect(page.locator('#send')).to_be_disabled()
    pending[0].fulfill(json={'state':state})
    expect(page.locator('#task')).to_be_enabled()
    expect(page.locator('#send')).to_be_enabled()
