"""User workflows for the quiet composer, reading, and inspection surfaces."""
from __future__ import annotations

import threading

import pytest
from playwright.sync_api import expect, sync_playwright

from codey.app import server


@pytest.fixture(scope="module")
def ui_browser(tmp_path_factory):
    state = server.AppContext(tmp_path_factory.mktemp("ui-workflow") / "state")
    original = server.STATE
    server.STATE = state
    httpd = server.CodeyHTTPServer(("127.0.0.1", 0), server.Handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=True)
            yield browser, httpd.launch_url(f"http://127.0.0.1:{httpd.server_port}/")
            browser.close()
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=2)
        state.close()
        server.STATE = original


@pytest.fixture
def page(ui_browser):
    browser, url = ui_browser
    page = browser.new_page(viewport={"width": 1280, "height": 800})
    page.add_init_script("window.EventSource = class { static OPEN = 1; readyState = 1; close() {} };")
    catalog = [{"id": key, "label": label, "available": True} for key, label in
               [("deepseek", "DeepSeek"), ("mimo", "MiMo"), ("stepfun", "StepFun"),
                ("qwen", "Qwen"), ("glm", "GLM"), ("local", "Local")]]
    state = {"active_id": "a", "sessions": [
        {"id": "a", "title": "Alpha", "provider": "deepseek", "messages": []},
        {"id": "b", "title": "Beta", "provider": "mimo", "messages": []}], "projects": []}

    def api(route):
        path = route.request.url.split("/api/")[-1].split("?")[0]
        data = {"ok": True}
        if path in {"provider_catalog", "providers"}:
            data = {"providers": catalog, "default": "deepseek"}
        elif path == "ui_state" and route.request.method == "GET":
            data = {"state": state}
        elif path == "pick_folder":
            data = {"ok": True, "path": "E:/project-alpha", "name": "Project Alpha"}
        route.fulfill(json=data)

    page.route("**/api/**", api)
    page.goto(url)
    # Desktop provider probes / other suites can compete for browser startup.
    # Readiness is a functional condition, not a five-second performance budget.
    expect(page.locator("#provider-button")).to_be_enabled(timeout=15000)
    page.wait_for_function("window.CodeyUiState.current().active_id === 'a'")
    yield page
    page.close()


def test_long_answer_is_visible_and_pointer_selection_survives_tool_update(page):
    text = "可选择的正文 Reading code safely.\n" * 25 + "FINAL CONCLUSION"
    page.evaluate("text => { addToSession('a', {type:'asst', text}); }", text)
    body = page.locator(".msg.asst .body")
    expect(body).to_contain_text("FINAL CONCLUSION")
    assert "collapsed" not in body.get_attribute("class")
    page.locator("#chat-area").evaluate("e => e.scrollTop = 0")
    first = body.locator("p").first.bounding_box()
    page.mouse.move(first["x"] + 2, first["y"] + 8)
    page.mouse.down()
    page.mouse.move(first["x"] + 200, first["y"] + 8, steps=15)
    page.mouse.up()
    selected = page.evaluate("getSelection().toString()")
    assert selected
    page.evaluate("addToSession('a', {type:'tool',kind:'read',path:'a.py',result:'2 lines'})")
    assert page.evaluate("getSelection().toString()") == selected
    expect(page.get_by_role("button", name="Collapse", exact=True)).to_be_visible()


def test_chat_drafts_and_caret_are_isolated(page):
    task = page.locator("#task")
    task.fill("alpha draft")
    page.evaluate("document.getElementById('task').setSelectionRange(2, 5)")
    page.evaluate("switchSession('b')")
    expect(task).to_have_value("")
    task.fill("beta draft")
    page.evaluate("switchSession('a')")
    expect(task).to_have_value("alpha draft")
    assert page.evaluate("[document.getElementById('task').selectionStart, document.getElementById('task').selectionEnd]") == [2, 5]
    page.evaluate("switchSession('b')")
    expect(task).to_have_value("beta draft")


def test_send_failure_keeps_draft_and_blocks_double_submission(page):
    page.evaluate("""() => {
        window.runCalls = 0;
        const realFetch = fetch;
        window.fetch = (url, options) => url === '/api/run'
            ? (runCalls++, new Promise(resolve => window.finishSend = () => resolve(new Response('{}', {status:409}))))
            : realFetch(url, options);
    }""")
    page.locator("#task").fill("keep this task")
    page.locator("#send").click()
    expect(page.locator("#task")).to_have_value("keep this task")
    expect(page.locator("#send")).to_be_disabled()
    page.locator("#task").press("Enter")
    assert page.evaluate("runCalls") == 1
    page.evaluate("finishSend()")
    expect(page.locator("#send")).to_be_enabled()
    expect(page.locator("#task")).to_have_value("keep this task")
    expect(page.locator(".status-row.err")).to_contain_text("Could not send")


def test_accepted_snapshot_does_not_erase_later_input_or_other_chat(page):
    page.evaluate("""() => {
        const realFetch = fetch;
        window.fetch = (url, options) => url === '/api/run'
            ? new Promise(resolve => window.finishSend = () => resolve(new Response('{"run_id":"run-a"}')))
            : realFetch(url, options);
    }""")
    page.locator("#task").fill("first task")
    page.locator("#send").click()
    page.locator("#task").fill("later edit")
    page.evaluate("switchSession('b')")
    page.locator("#task").fill("beta task")
    page.evaluate("finishSend()")
    page.wait_for_function("window.CodeyUiState.current().sessions[0].messages.some(m => m.type === 'user')")
    expect(page.locator("#task")).to_have_value("beta task")
    page.evaluate("switchSession('a')")
    expect(page.locator("#task")).to_have_value("later edit")


def test_choose_folder_never_sends_draft(page):
    calls = []
    page.on("request", lambda request: calls.append(request.url) if request.url.endswith("/api/run") else None)
    page.locator("#task").fill("do not run yet")
    page.locator("#ctx-folder").click()
    expect(page.locator("#ctx-folder")).to_have_text("Project Alpha")
    expect(page.locator("#task")).to_have_value("do not run yet")
    assert calls == []


def test_success_consumes_only_submitted_chat_draft(page):
    page.route("**/api/run", lambda route: route.fulfill(json={"run_id": "accepted"}))
    page.locator("#task").fill("accepted task")
    page.locator("#send").click()
    expect(page.locator("#task")).to_have_value("")
    page.evaluate("switchSession('b')")
    page.locator("#task").fill("independent")
    page.evaluate("switchSession('a')")
    expect(page.locator("#task")).to_have_value("")


def test_retry_preserves_new_draft_and_uses_original_failed_submission(page):
    submissions = []

    def run(route):
        submissions.append(route.request.post_data_json)
        route.fulfill(status=409, json={"error": "busy"})

    page.route("**/api/run", run)
    page.locator("#task").fill("original task")
    page.locator("#send").click()
    expect(page.locator(".status-row.err")).to_be_visible()
    page.locator("#task").fill("later draft")
    page.get_by_role("button", name="Retry", exact=True).click()
    expect(page.locator("#send")).to_be_enabled()
    assert len(submissions) == 2
    assert submissions[1]["task"] == "original task"
    expect(page.locator("#task")).to_have_value("later draft")


def test_model_menu_has_no_search_and_small_catalog_keyboard(page):
    page.locator("#provider-button").click()
    expect(page.locator("#provider-menu input")).to_have_count(0)
    expect(page.locator(".provider-item:visible")).to_have_count(6)
    page.keyboard.press("Enter")
    expect(page.locator("#provider-name")).to_have_text("DeepSeek")
    page.evaluate("CodeyProviderUI.applyConfig({providers:[{id:'deepseek',label:'DeepSeek'},{id:'mimo',label:'MiMo'}],default:'mimo'})")
    page.locator("#provider-button").click()
    expect(page.locator("#provider-menu input")).to_have_count(0)
    page.keyboard.press("ArrowDown")
    page.keyboard.press("Enter")
    expect(page.locator("#provider-name")).to_have_text("MiMo")
    assert page.evaluate("CodeyUiState.DEFAULT_PROVIDER") == "mimo"


def test_late_changes_response_cannot_replace_new_project_or_reopen_drawer(page):
    page.evaluate("""() => {
        const realFetch = fetch;
        window.fetch = (url, options) => {
            if (url !== '/api/changes') return realFetch(url, options);
            const project = JSON.parse(options.body).project;
            if (project === 'E:/old') return new Promise(resolve => window.finishChanges = () => resolve(new Response(JSON.stringify({ok:true,mode:'git',files:[{path:'old.py'}],diff:''}))));
            return Promise.resolve(new Response(JSON.stringify({ok:true,mode:'git',files:[{path:'new.py'}],diff:''})));
        };
        openChangesDrawer('E:/old');
    }""")
    page.evaluate("openChangesDrawer('E:/new')")
    expect(page.locator("#changes-body")).to_contain_text("new.py")
    page.evaluate("finishChanges()")
    expect(page.locator("#changes-body")).not_to_contain_text("old.py")
    page.evaluate("switchSession('b')")
    expect(page.locator("#changes-drawer")).to_have_attribute("aria-hidden", "true")


def test_change_file_link_expands_exact_file_and_escape_returns_focus(page):
    page.route("**/api/changes", lambda route: route.fulfill(json={"ok": True, "mode": "snapshot", "files": [
        {"path": "one.py"}, {"path": "two.py"}], "diff": "diff --git a/two.py b/two.py\n@@ -1 +1 @@\n-old\n+new"}))
    page.evaluate("addToSession('a',{type:'changes',project:'E:/project-alpha',files:[{path:'two.py'}]})")
    link = page.get_by_role("button", name="two.py", exact=True)
    link.click()
    expect(page.locator('.change-file[data-path="two.py"] pre')).to_be_visible()
    expect(page.locator('.change-file[data-path="one.py"] pre')).to_be_hidden()
    page.keyboard.press("Escape")
    expect(link).to_be_focused()


def test_late_restore_failure_stays_with_original_project(page):
    page.route("**/api/changes", lambda route: route.fulfill(json={"ok": True, "mode": "snapshot", "files": [{"path": "one.py"}], "diff": ""}))
    page.evaluate("""() => {
        const realFetch = fetch;
        window.fetch = (url, options) => url === '/api/changes/restore'
            ? new Promise(resolve => window.finishRestore = () => resolve(new Response('{"ok":false,"error":"OLD RESTORE ERROR"}')))
            : realFetch(url, options);
        openChangesDrawer('E:/old');
    }""")
    expect(page.locator("#changes-restore")).to_be_enabled()
    page.locator("#changes-restore").click()
    page.evaluate("openChangesDrawer('E:/new')")
    expect(page.locator("#changes-scope")).to_have_text("new")
    page.evaluate("finishRestore()")
    page.wait_for_function("document.getElementById('changes-restore').textContent !== 'Restoring'")
    expect(page.locator("#changes-subtitle")).not_to_contain_text("OLD RESTORE ERROR")


def test_narrow_window_controls_can_be_clicked_and_keyboard_sidebar_menu_closes(page):
    page.set_viewport_size({"width": 480, "height": 420})
    page.locator("#provider-button").click()
    expect(page.locator('.provider-item[data-provider="deepseek"]')).to_be_focused()
    page.keyboard.press("Escape")
    page.locator("#show-sidebar").click()
    title = page.locator(".session-title").first
    title.focus()
    page.keyboard.press("Tab")
    expect(page.locator(".session-more").first).to_be_focused()
    page.keyboard.press("Enter")
    page.keyboard.press("ArrowDown")
    page.keyboard.press("Escape")
    expect(page.locator(".session-more").first).to_be_focused()


def test_full_refresh_preserves_selection_and_collapsed_answer(page):
    page.evaluate("""() => {
        addToSession('a', {type:'asst',text:'Full answer\\n'.repeat(30)});
        const range = document.createRange(); range.selectNodeContents(document.querySelector('.msg.asst .body'));
        getSelection().removeAllRanges(); getSelection().addRange(range);
        window.selectedAnswer = getSelection().toString();
        renderChat();
    }""")
    assert page.evaluate("getSelection().toString() === selectedAnswer")
    page.get_by_role("button", name="Collapse", exact=True).click()
    page.evaluate("switchSession('b'); switchSession('a')")
    expect(page.get_by_role("button", name="Expand", exact=True)).to_be_visible()


def test_model_menu_keyboard_and_focus_return(page):
    trigger = page.locator("#provider-button")
    trigger.click()
    expect(trigger).to_have_attribute("aria-expanded", "true")
    expect(page.locator('.provider-item[data-provider="deepseek"]')).to_be_focused()
    for _ in range(3):
        page.keyboard.press("ArrowDown")
    page.keyboard.press("Enter")
    expect(page.locator("#provider-name")).to_have_text("Qwen")
    expect(trigger).to_be_focused()
    trigger.click()
    page.keyboard.press("Escape")
    expect(trigger).to_have_attribute("aria-expanded", "false")
    expect(trigger).to_be_focused()


def test_running_owner_and_approval_are_discoverable_without_enter_approving(page):
    approvals = []
    page.on("request", lambda request: approvals.append(request.url) if "/api/shell_approval" in request.url else None)
    page.evaluate("""() => {
        applyRunState({busy:true,run_id:'run-a',session_id:'a'});
        addToSession('a', {type:'shell_request',id:'approval-a',command:'pytest -q',cwd:'E:/project-alpha'});
        switchSession('b');
    }""")
    expect(page.locator("#composer-notice")).to_contain_text("Approval required")
    expect(page.locator("#status")).to_contain_text("Alpha")
    expect(page.locator("#stop")).to_have_attribute("aria-label", "Stop Alpha")
    page.locator("#task").fill("another draft")
    page.locator("#task").press("Enter")
    assert approvals == []
    page.get_by_role("button", name="Review command", exact=True).click()
    expect(page.locator("#sess-title")).to_contain_text("Alpha")
    expect(page.locator(".shell-command")).to_contain_text("pytest -q")


def test_reading_position_and_back_to_latest(page):
    page.evaluate("""() => {
        for (let i=0; i<40; i++) addToSession('a',{type:'asst',text:'Reading paragraph '+i});
        document.getElementById('chat-area').scrollTop = 200;
        document.getElementById('chat-area').dispatchEvent(new Event('scroll'));
        switchSession('b'); switchSession('a');
    }""")
    assert abs(page.locator("#chat-area").evaluate("e => e.scrollTop") - 200) < 4
    page.evaluate("addToSession('a', {type:'asst',text:'New output'})")
    expect(page.get_by_role("button", name="Back to latest", exact=True)).to_be_visible()
    page.get_by_role("button", name="Back to latest", exact=True).click()
    assert page.locator("#chat-area").evaluate("e => e.scrollHeight - e.scrollTop - e.clientHeight") < 4


def test_search_and_inspection_scope_close_on_chat_switch(page):
    trigger = page.get_by_role("button", name="Search chats", exact=True)
    search = page.get_by_role("searchbox", name="Search chats")
    expect(search).to_be_hidden()
    before = trigger.bounding_box()
    new_chat = page.locator("#btn-new-chat").bounding_box()
    add_project = page.locator("#btn-add-project").bounding_box()
    first_gap = add_project["y"] - new_chat["y"] - new_chat["height"]
    second_gap = before["y"] - add_project["y"] - add_project["height"]
    assert abs(first_gap - second_gap) < 1
    trigger.click()
    expect(search).to_be_focused()
    after = search.bounding_box()
    assert abs(before["height"] - after["height"]) < 1
    assert abs(before["y"] - after["y"]) < 1
    search.fill("beta")
    expect(page.locator(".session-title")).to_have_text("Beta")
    search.press("Escape")
    expect(search).to_be_hidden()
    expect(trigger).to_be_focused()
    expect(page.locator(".session-title")).to_have_count(2)
    page.route("**/api/changes", lambda route: route.fulfill(json={"ok": True, "mode": "git", "files": [
        {"path": "module.py", "status": "M", "additions": 1}],
        "diff": "diff --git a/module.py b/module.py\n@@ -1 +1 @@\n-old\n+new"}))
    page.evaluate("openChangesDrawer('E:/project-alpha')")
    expect(page.locator("#changes-subtitle")).to_contain_text("Working tree")
    expect(page.locator("#changes-drawer")).to_contain_text("project-alpha")
    page.evaluate("switchSession('b')")
    expect(page.locator("#changes-drawer")).to_have_attribute("aria-hidden", "true")
    assert page.locator("#changes-drawer").evaluate("e => e.inert")


@pytest.mark.parametrize("size", [(1280, 800), (720, 560), (480, 420)])
def test_composer_controls_share_frame_and_remain_reachable(page, size):
    page.set_viewport_size({"width": size[0], "height": size[1]})
    page.locator("#task").fill("long input\n" * 25)
    box = page.locator(".composer-box").bounding_box()
    for control in [page.locator("#provider-button"), page.locator("#send")]:
        rect = control.bounding_box()
        assert rect["y"] >= box["y"] and rect["y"] + rect["height"] <= box["y"] + box["height"] + 1
        assert rect["x"] >= 0 and rect["x"] + rect["width"] <= size[0]
        assert rect["y"] + rect["height"] <= size[1]
    assert page.locator("#send").bounding_box()["width"] >= 32
    hint = page.locator("#send-hint").bounding_box()
    send = page.locator("#send").bounding_box()
    assert 0 <= send["x"] - hint["x"] - hint["width"] <= 12
