"""Recovery actions and search results exercised through the shipped UI."""
from __future__ import annotations

from unittest import mock

import pytest
from playwright.sync_api import expect

from codey.app import api
from tests.test_ui_workflow import model_settings_route
from tests.test_ui_workflow import page as page
from tests.test_ui_workflow import ui_browser as ui_browser


@pytest.mark.parametrize("failure", ["http", "network", "invalid_json"])
def test_settings_load_failure_can_retry_in_same_dialog(page, failure):
    local = model_settings_route(page)
    requests = []

    def connection(route):
        requests.append(route.request.method)
        if len(requests) > 1:
            route.fulfill(json={"ok": True, "local": local})
        elif failure == "network":
            route.abort()
        elif failure == "invalid_json":
            route.fulfill(body="not json", content_type="text/plain")
        else:
            route.fulfill(status=503, json={"error": "internal detail"})

    page.route("**/api/local_provider", connection)
    page.locator("#btn-settings").click()
    dialog = page.get_by_role("dialog", name="Settings", exact=True)
    expect(dialog).to_be_visible()
    expect(page.locator("#local-config-summary")).to_have_text("Could not load connection")
    expect(page.locator("#local-config-error")).to_be_empty()
    expect(page.locator("#local-base-url")).to_be_disabled()
    expect(page.locator("#local-config-save")).to_be_disabled()
    retry = dialog.get_by_role("button", name="Retry", exact=True)
    expect(retry).to_be_enabled()
    retry.click()
    expect(page.locator("#local-base-url")).to_have_value(local["base_url"])
    expect(page.locator("#local-config-save")).to_be_enabled()
    expect(retry).to_be_hidden()
    expect(page.locator("#local-base-url")).to_be_focused()
    assert requests == ["GET", "GET"]
    expect(dialog).to_be_visible()
    page.keyboard.press("Escape")
    expect(page.locator("#btn-settings")).to_be_focused()


def test_settings_retry_is_single_flight_and_closed_dialog_ignores_late_reply(page):
    local = model_settings_route(page)
    page.route("**/api/local_provider", lambda route: route.fulfill(status=503, json={}))
    page.locator("#btn-settings").click()
    dialog = page.get_by_role("dialog", name="Settings", exact=True)
    retry = dialog.get_by_role("button", name="Retry", exact=True)
    expect(retry).to_be_enabled()
    page.evaluate("""local => {
        const realFetch = fetch;
        window.retryLoads = 0;
        window.fetch = (url, options) => url === '/api/local_provider'
            ? (retryLoads++, new Promise(resolve => window.finishConnection = () =>
                resolve(new Response(JSON.stringify({ok:true,local})))) )
            : realFetch(url, options);
    }""", local)
    retry.click()
    expect(page.locator("#local-config-summary")).to_have_text("Loading connection…")
    expect(retry).to_be_disabled()
    page.evaluate("document.getElementById('local-config-retry').click()")
    assert page.evaluate("retryLoads") == 1
    page.keyboard.press("Escape")
    page.evaluate("finishConnection()")
    expect(dialog).to_be_hidden()
    expect(page.locator("#btn-settings")).to_be_focused()


def test_settings_save_failure_keeps_edit_and_never_shows_load_retry(page):
    model_settings_route(page)
    page.locator("#btn-settings").click()
    expect(page.locator("#local-config-save")).to_be_enabled()
    page.locator("#local-model-name").fill("my-edited-model")
    page.route("**/api/local_provider", lambda route: route.fulfill(
        status=400, json={"ok": False, "error": "Model is unavailable"},
    ))
    page.locator("#local-config-save").click()
    expect(page.locator("#local-config-error")).to_have_text("Model is unavailable")
    expect(page.locator("#local-model-name")).to_have_value("my-edited-model")
    expect(page.locator("#local-config-retry")).to_be_hidden()
    expect(page.locator("#local-config-save")).to_be_enabled()


def search_state(page):
    state = {
        "active_id": "a", "projects": [
            {"id": "empty", "name": "alpha-empty", "path": "E:/alpha-empty", "expanded": False},
            {"id": "populated", "name": "beta-project", "path": "E:/beta", "expanded": False},
        ], "sessions": [
            {"id": "a", "title": "Loose chat", "provider": "deepseek", "messages": []},
            {"id": "b", "title": "Fix failing test", "projectId": "populated",
             "provider": "mimo", "messages": []},
        ],
    }
    page.evaluate("state => {CodeyUiState.apply(state); renderSidebar(); renderChat();}", state)
    page.locator("#chat-search-toggle").click()
    return page.locator("#chat-search")


def test_search_finds_empty_project_and_keeps_saved_expansion(page):
    search = search_state(page)
    search.fill("ALPHA")
    expect(page.locator(".project-main")).to_have_text("alpha-empty")
    expect(page.locator(".project-children")).to_have_text("No chats")
    expect(page.locator(".project-toggle path")).to_have_attribute("d", "m6 9 6 6 6-6")
    expect(page.locator("#search-empty")).to_be_hidden()
    expect(page.locator(".group-label:visible")).to_have_text("PROJECTS")
    assert page.evaluate("CodeyUiState.current().projects[0].expanded") is False
    search.press("Escape")
    expect(search).to_be_hidden()
    expect(page.locator("#chat-search-toggle")).to_be_focused()
    expect(page.locator(".project-main")).to_have_count(2)
    expect(page.locator(".project-children")).to_have_count(0)
    expect(page.locator(".project-toggle path").first).to_have_attribute("d", "m9 6 6 6-6 6")


def test_search_no_matches_is_one_empty_state_and_updates_after_rename(page):
    search = search_state(page)
    settings_y = page.locator("#btn-settings").bounding_box()["y"]
    search.fill("missing-name")
    expect(page.locator("#search-empty")).to_have_text("No matches")
    expect(page.locator("#search-empty")).to_be_visible()
    expect(page.locator("#session-list")).to_be_hidden()
    assert abs(page.locator("#btn-settings").bounding_box()["y"] - settings_y) < 1
    page.evaluate("CodeyUiState.current().projects[0].name='missing-name'; renderSidebar();")
    expect(page.locator("#search-empty")).to_be_hidden()
    expect(page.locator("#session-list")).to_be_visible()
    expect(page.locator(".project-main")).to_have_text("missing-name")


def test_search_project_name_and_chat_title_keep_only_matching_groups(page):
    search = search_state(page)
    search.fill("beta-project")
    expect(page.locator(".session-title")).to_have_text("Fix failing test")
    expect(page.locator(".project-main")).to_have_text("beta-project")
    expect(page.locator(".group-label:visible")).to_have_text("PROJECTS")
    search.fill("failing")
    expect(page.locator(".project-main")).to_have_text("beta-project")
    search.fill("Loose")
    expect(page.locator(".project-main")).to_have_count(0)
    expect(page.locator(".group-label:visible")).to_have_text("CHATS")
    expect(page.locator(".session-title")).to_have_text("Loose chat")


def test_search_accessible_name_explains_both_objects_without_changing_style(page):
    trigger = page.get_by_role("button", name="Search chats and projects", exact=True)
    expect(trigger).to_have_text("Search")
    box = trigger.bounding_box()
    trigger.click()
    search = page.get_by_role("searchbox", name="Search chats and projects", exact=True)
    expect(search).to_be_focused()
    assert abs(search.bounding_box()["height"] - box["height"]) < 1
    assert search.evaluate("e => getComputedStyle(e).borderTopWidth") == "0px"


def test_search_clear_uses_neutral_outline_icon_in_default_and_hover_states(page):
    search = search_state(page)
    search.fill("alpha")
    clear = page.get_by_role("button", name="Clear search", exact=True)
    expect(clear).to_be_visible()
    page.mouse.move(500, 300)
    assert clear.evaluate("e => getComputedStyle(e).color") == "rgb(160, 160, 160)"
    assert clear.evaluate("e => getComputedStyle(e).backgroundColor") == "rgba(0, 0, 0, 0)"
    assert clear.evaluate("e => getComputedStyle(e).borderTopWidth") == "0px"
    icon = clear.locator("svg")
    assert icon.evaluate("e => getComputedStyle(e).stroke") == "rgb(160, 160, 160)"
    assert icon.evaluate("e => getComputedStyle(e).fill") == "none"
    clear.hover()
    assert clear.evaluate("e => getComputedStyle(e).color") == "rgb(230, 230, 230)"
    assert icon.evaluate("e => getComputedStyle(e).stroke") == "rgb(230, 230, 230)"
    assert search.bounding_box()["height"] == 34


@pytest.mark.parametrize("keyboard", [False, True])
def test_search_clear_restores_results_and_returns_focus_to_open_input(page, keyboard):
    search = search_state(page)
    clear = page.get_by_role("button", name="Clear search", exact=True)
    expect(clear).to_be_hidden()
    search.fill("alpha")
    if keyboard:
        search.press("Tab")
        expect(clear).to_be_focused()
        clear.press("Enter")
    else:
        clear.click()
    expect(search).to_have_value("")
    expect(search).to_be_visible()
    expect(search).to_be_focused()
    expect(clear).to_be_hidden()
    expect(page.locator(".project-main")).to_have_count(2)
    expect(page.locator("#search-empty")).to_be_hidden()
    search.fill("alpha")
    search.press("Escape")
    expect(clear).to_be_hidden()
    expect(page.locator("#chat-search-toggle")).to_be_focused()


@pytest.mark.parametrize("width", [1280, 480])
def test_search_clear_fits_long_query_in_wide_and_narrow_sidebar(page, width):
    page.set_viewport_size({"width": width, "height": 800})
    if width <= 720:
        page.locator("#show-sidebar").click()
    search = search_state(page)
    search.fill("a long project search " * 20)
    clear = page.get_by_role("button", name="Clear search", exact=True)
    expect(clear).to_be_visible()
    input_box, clear_box = search.bounding_box(), clear.bounding_box()
    assert clear_box["x"] >= input_box["x"]
    assert clear_box["x"] + clear_box["width"] <= input_box["x"] + input_box["width"]
    assert clear_box["y"] >= input_box["y"]
    assert clear_box["y"] + clear_box["height"] <= input_box["y"] + input_box["height"]
    assert search.evaluate("e => parseFloat(getComputedStyle(e).paddingRight)") >= clear_box["width"]
    clear.click()
    search.press("Tab")
    expect(search).to_be_hidden()
    expect(clear).to_be_hidden()


def test_busy_rejection_opens_actual_running_chat_and_preserves_draft(page):
    page.route("**/api/run", lambda route: route.fulfill(status=409, json={"error": "busy"}))
    page.route("**/api/state", lambda route: route.fulfill(json={
        "busy": True, "run_id": "run-b", "session_id": "b", "run_status": "running",
    }))
    page.locator("#task").fill("original task")
    page.locator("#send").click()
    error = page.locator(".status-row.err")
    expect(error).to_contain_text("Another chat is running")
    expect(page.locator("#task")).to_have_value("original task")
    page.locator("#task").fill("later alpha draft")
    error.get_by_role("button", name="Open", exact=True).click()
    expect(page.locator("#sess-title")).to_have_text("Beta")
    page.locator("#task").fill("beta draft")
    page.evaluate("switchSession('a')")
    expect(page.locator("#task")).to_have_value("later alpha draft")


@pytest.mark.parametrize(("status", "body", "expected"), [
    (503, {"error": "browser worker busy", "hint": "retry"}, "Codey is temporarily busy"),
    (500, {"error": "secret internal details"}, "Could not send the message"),
    (409, {"error": "unrelated conflict", "hint": "try_continue"}, "Could not send the message"),
])
def test_retry_uses_original_submission_without_leaking_unknown_errors(page, status, body, expected):
    submissions = []

    def run(route):
        submissions.append(route.request.post_data_json)
        route.fulfill(status=status, json=body)

    page.route("**/api/run", run)
    page.locator("#task").fill("original task")
    page.locator("#send").click()
    error = page.locator(".status-row.err").first
    expect(error).to_contain_text(expected)
    expect(error).not_to_contain_text("secret")
    page.locator("#task").fill("later draft")
    error.get_by_role("button", name="Retry", exact=True).click()
    expect(page.locator(".status-row.err")).to_have_count(2)
    assert submissions[1]["task"] == "original task"
    expect(page.locator("#task")).to_have_value("later draft")


def test_local_rejection_immediately_offers_enabled_model_action(page):
    local = model_settings_route(page)
    page.evaluate("CodeyProviderUI.applyLocalMetadata", local)
    page.locator("#provider-button").click()
    page.locator('.provider-item[data-provider="local"]').first.click()
    submissions = []

    def reject(route):
        submissions.append(route.request.post_data_json)
        route.fulfill(status=400, json={"reason": "local_selection_invalid"})

    page.route("**/api/run", reject)
    page.locator("#task").fill("original local task")
    page.locator("#send").click()
    choose = page.locator(".status-row.err").get_by_role("button", name="Choose model", exact=True)
    expect(choose).to_be_enabled()
    choose.click()
    expect(page.locator("#provider-menu")).to_be_visible()
    expect(page.locator("#task")).to_have_value("original local task")
    assert len(submissions) == 1


def test_local_rejection_choose_model_returns_to_original_chat_without_sending(page):
    local = model_settings_route(page)
    page.evaluate("CodeyProviderUI.applyLocalMetadata", local)
    page.locator("#provider-button").click()
    page.locator('.provider-item[data-provider="local"]').first.click()
    page.evaluate("""() => {
        const realFetch = fetch;
        window.runCalls = 0;
        window.fetch = (url, options) => url === '/api/run'
            ? (runCalls++, new Promise(resolve => window.finishRejectedSend = () => resolve(
                new Response(JSON.stringify({reason:'local_selection_invalid',error:'private details'}),{status:400}))))
            : realFetch(url, options);
    }""")
    page.locator("#task").fill("original local task")
    page.locator("#send").click()
    page.evaluate("switchSession('b')")
    page.locator("#task").fill("beta draft")
    page.evaluate("finishRejectedSend()")
    page.wait_for_function("CodeyUiState.current().sessions.find(s=>s.id==='a').messages.some(m=>m.type==='err')")
    expect(page.locator("#task")).to_have_value("beta draft")
    page.evaluate("switchSession('a')")
    error = page.locator(".status-row.err")
    expect(error).to_contain_text("Select the local model again")
    expect(error.get_by_role("button", name="Retry", exact=True)).to_have_count(0)
    page.evaluate("switchSession('b')")
    # The receipt action retains its own chat even if invoked after navigation.
    page.evaluate("""() => {
        const m=CodeyUiState.current().sessions.find(s=>s.id==='a').messages.find(m=>m.type==='err');
        const node=document.createElement('div'); appendMessageNode(node,m);
        node.querySelector('button').click();
    }""")
    expect(page.locator("#sess-title")).to_have_text("Alpha")
    expect(page.locator("#provider-menu")).to_be_visible()
    expect(page.locator("#task")).to_have_value("original local task")
    assert page.evaluate("runCalls") == 1
    page.keyboard.press("Escape")
    page.evaluate("switchSession('b')")
    expect(page.locator("#task")).to_have_value("beta draft")


def test_invalid_local_selection_has_stable_recovery_reason_and_is_not_submitted():
    submit = mock.Mock()
    with mock.patch("codey.providers.local_selection.capture_local_run_config", side_effect=ValueError("changed")):
        status, payload = api.run_submit_response({
            "task": "hello", "provider": "local", "local_selection": {"model": "old"},
        }, submit)
    assert status == 400
    assert payload["reason"] == "local_selection_invalid"
    submit.assert_not_called()
