"""Topbar title editing through the shipped UI, including ongoing work."""
from __future__ import annotations

import copy

import pytest
from playwright.sync_api import expect

from codey.storage.ui_state_store import UiStateStore
from tests.test_ui_workflow import page as page
from tests.test_ui_workflow import ui_browser as ui_browser


def title_button(page):
    return page.locator("#sess-title").get_by_role("button", name="Rename chat", exact=True)


def start_rename(page):
    button = title_button(page)
    expect(button).to_be_visible()
    button.click()
    editor = page.locator("#sess-title").get_by_role("textbox", name="Chat title", exact=True)
    expect(editor).to_be_focused()
    return editor


def record_state_saves(page):
    writes = []
    restored = {"state": page.evaluate("JSON.parse(JSON.stringify(CodeyUiState.current()))")}

    def api(route):
        if route.request.method == "POST":
            payload = route.request.post_data_json
            writes.append(copy.deepcopy(payload["state"]))
            restored["state"] = copy.deepcopy(payload["state"])
            restored["state"]["revision"] = payload["base_revision"] + 1
            route.fulfill(json={"ok": True, "revision": restored["state"]["revision"]})
        else:
            route.fulfill(json={"ok": True, "state": restored["state"]})

    page.route("**/api/ui_state", api)
    return writes


def test_title_click_selects_original_and_enter_saves_both_names_and_reload(page):
    writes = record_state_saves(page)
    bar = page.locator(".topbar").bounding_box()
    editor = start_rename(page)
    assert editor.evaluate("e => [e.selectionStart,e.selectionEnd]") == [0, 5]
    expect(editor).to_have_attribute("maxlength", "80")
    editor.fill("  检查缓存问题  ")
    expect(page.locator(".session-item.active .session-title")).to_have_text("Alpha")
    assert page.evaluate("CodeyUiState.current().sessions[0].title") == "Alpha"
    assert writes == []
    editor.press("Enter")
    expect(title_button(page)).to_have_text("检查缓存问题")
    expect(title_button(page)).to_be_focused()
    expect(page.locator(".session-item.active .session-title")).to_have_text("检查缓存问题")
    page.wait_for_function("JSON.parse(localStorage.getItem('codey:sessions'))[0].title === '检查缓存问题'")
    assert len(writes) == 1
    assert writes[0]["sessions"][0]["title"] == "检查缓存问题"
    assert page.locator(".topbar").bounding_box() == bar
    page.reload()
    expect(title_button(page)).to_have_text("检查缓存问题")
    expect(page.locator(".session-item.active .session-title")).to_have_text("检查缓存问题")


def test_title_editor_is_transparent_borderless_and_keeps_original_text_alignment(page):
    button = title_button(page)
    text_left = button.evaluate("e => e.getBoundingClientRect().left + parseFloat(getComputedStyle(e).paddingLeft)")
    bar = page.locator(".topbar").bounding_box()
    editor = start_rename(page)
    expect(editor).to_have_css("background-color", "rgba(0, 0, 0, 0)")
    for side in ["top", "right", "bottom", "left"]:
        expect(editor).to_have_css(f"border-{side}-width", "0px")
    expect(editor).to_have_css("box-shadow", "none")
    expect(editor).to_have_css("outline-width", "0px")
    expect(editor).to_have_css("appearance", "none")
    editor_text_left = editor.evaluate("e => e.getBoundingClientRect().left + parseFloat(getComputedStyle(e).paddingLeft)")
    assert abs(editor_text_left - text_left) < 1
    assert page.locator(".topbar").bounding_box() == bar
    editor.fill("Natural rename")
    editor.press("Enter")
    expect(title_button(page)).to_have_text("Natural rename")


@pytest.mark.parametrize("value", ["", "   ", "Discard this edit"])
def test_escape_or_blank_preserves_original_without_save(page, value):
    writes = record_state_saves(page)
    editor = start_rename(page)
    editor.fill(value)
    editor.press("Escape" if value.strip() else "Enter")
    expect(title_button(page)).to_have_text("Alpha")
    expect(title_button(page)).to_be_focused()
    expect(page.locator(".session-item.active .session-title")).to_have_text("Alpha")
    assert writes == []


def test_outside_click_saves_and_preserves_composer_draft_and_clicked_focus(page):
    task = page.locator("#task")
    task.fill("keep my draft")
    editor = start_rename(page)
    editor.fill("Saved on blur")
    task.click()
    expect(title_button(page)).to_have_text("Saved on blur")
    expect(task).to_be_focused()
    expect(task).to_have_value("keep my draft")
    expect(page.locator(".session-item.active .session-title")).to_have_text("Saved on blur")


def test_keyboard_and_collapsed_sidebar_can_rename_current_chat(page):
    page.locator("#toggle-sidebar").click()
    page.locator("#show-sidebar").focus()
    page.keyboard.press("Tab")
    expect(title_button(page)).to_be_focused()
    page.keyboard.press("Enter")
    editor = page.get_by_role("textbox", name="Chat title", exact=True)
    expect(editor).to_be_focused()
    editor.fill("Keyboard name")
    editor.press("Enter")
    expect(title_button(page)).to_have_text("Keyboard name")
    expect(title_button(page)).to_be_focused()
    assert page.locator("body").evaluate("e => e.classList.contains('sidebar-collapsed')")
    page.locator("#show-sidebar").click()
    expect(page.locator(".session-item.active .session-title")).to_have_text("Keyboard name")


def test_composition_enter_escape_and_229_do_not_finish_rename(page):
    editor = start_rename(page)
    editor.dispatch_event("compositionstart")
    editor.fill("中文名称")
    for event in [{"key": "Enter", "isComposing": True}, {"key": "Enter", "keyCode": 229}, {"key": "Escape"}]:
        editor.dispatch_event("keydown", event)
        expect(editor).to_be_focused()
        expect(editor).to_have_value("中文名称")
        expect(page.locator(".session-item.active .session-title")).to_have_text("Alpha")
    editor.dispatch_event("compositionend")
    editor.press("Enter")
    expect(title_button(page)).to_have_text("中文名称")


def test_composition_finishing_during_pointer_down_does_not_swallow_chat_switch(page):
    editor = start_rename(page)
    editor.fill("中文旧聊天名称")
    editor.dispatch_event("compositionstart")
    beta = page.get_by_role("button", name="Beta", exact=True).bounding_box()
    page.mouse.move(beta["x"] + 10, beta["y"] + beta["height"] / 2)
    page.mouse.down()
    expect(editor).to_be_visible()
    editor.dispatch_event("compositionend")
    expect(editor).to_be_visible()
    page.mouse.up()
    expect(title_button(page)).to_have_text("Beta")
    assert page.evaluate("CodeyUiState.current().sessions.map(s=>s.title)") == ["中文旧聊天名称", "Beta"]


def test_refresh_and_running_reply_preserve_editor_identity_caret_and_task(page):
    page.locator("#task").fill("draft during run")
    page.evaluate("runningSessionId='a'; setStatus('Running','run'); updateSend();")
    editor = start_rename(page)
    editor.fill("Editing during work")
    editor.evaluate("e => {e.setSelectionRange(3,7); window.originalTitleEditor=e;}")
    page.evaluate("addToSession('a',{type:'asst',text:'New reply'}); renderSidebar(); renderChat();")
    assert editor.evaluate("e => e === window.originalTitleEditor")
    expect(editor).to_be_focused()
    expect(editor).to_have_value("Editing during work")
    assert editor.evaluate("e => [e.selectionStart,e.selectionEnd]") == [3, 7]
    editor.press("Enter")
    expect(title_button(page)).to_have_text("Editing during work")
    assert page.evaluate("runningSessionId") == "a"
    expect(page.locator(".msg.asst .body")).to_contain_text("New reply")
    expect(page.locator("#task")).to_have_value("draft during run")


@pytest.mark.parametrize("pointer", [True, False])
def test_chat_switch_saves_origin_without_losing_click_or_renaming_destination(page, pointer):
    editor = start_rename(page)
    editor.fill("Renamed Alpha")
    editor.evaluate("e => {window.oldTitleEditor=e}")
    if pointer:
        page.get_by_role("button", name="Beta", exact=True).click()
    else:
        page.evaluate("switchSession('b')")
    expect(title_button(page)).to_have_text("Beta")
    assert page.evaluate("CodeyUiState.current().active_id") == "b"
    assert page.evaluate("CodeyUiState.current().sessions.map(s => s.title)") == ["Renamed Alpha", "Beta"]
    # Late events from a removed input must not finish a new chat's edit.
    next_editor = start_rename(page)
    next_editor.fill("Beta draft")
    page.evaluate("oldTitleEditor.dispatchEvent(new Event('blur')); oldTitleEditor.dispatchEvent(new KeyboardEvent('keydown',{key:'Enter'}))")
    expect(next_editor).to_have_value("Beta draft")
    next_editor.press("Escape")
    expect(title_button(page)).to_have_text("Beta")


def test_only_chat_breadcrumb_is_editable_and_title_fits_narrow_window(page):
    page.evaluate("""() => {
        const state=CodeyUiState.current();
        state.projects.push({id:'project',name:'Project name',path:'E:/project',expanded:true});
        state.sessions[0].projectId='project'; renderSidebar(); renderChat();
    }""")
    page.locator(".crumb-proj").click()
    expect(page.get_by_role("textbox", name="Chat title", exact=True)).to_have_count(0)
    container = page.locator("#sess-title").bounding_box()
    button = title_button(page).bounding_box()
    page.mouse.click(container["x"] + container["width"] - 5, container["y"] + container["height"] / 2)
    expect(page.get_by_role("textbox", name="Chat title", exact=True)).to_have_count(0)
    assert button["width"] < container["width"] / 2
    page.set_viewport_size({"width": 480, "height": 800})
    editor = start_rename(page)
    editor.fill("x" * 100)
    editor.press("Enter")
    expect(title_button(page)).to_have_text("x" * 80)
    expect(page.locator(".crumb-proj")).to_have_text("Project name")
    assert title_button(page).bounding_box()["x"] + title_button(page).bounding_box()["width"] <= page.locator("#topbar-more").bounding_box()["x"]


def test_late_composition_event_cannot_finish_new_editor_during_chat_click(page):
    editor = start_rename(page)
    editor.fill("Original chat name")
    editor.evaluate("e => {window.oldCompositionEditor=e}")
    page.evaluate("switchSession('b')")
    next_editor = start_rename(page)
    next_editor.fill("New Beta name")
    target = page.get_by_role("button", name="Original chat name", exact=True).bounding_box()
    page.mouse.move(target["x"] + 10, target["y"] + target["height"] / 2)
    page.mouse.down()
    try:
        page.evaluate("oldCompositionEditor.dispatchEvent(new CompositionEvent('compositionend'))")
        expect(next_editor).to_be_visible()
        expect(next_editor).to_have_value("New Beta name")
    finally:
        page.mouse.up()
    expect(title_button(page)).to_have_text("Original chat name")
    assert page.evaluate("CodeyUiState.current().sessions.map(s=>s.title)") == ["Original chat name", "New Beta name"]


def test_top_menu_has_no_rename_but_sidebar_keeps_other_chat_rename(page):
    page.locator("#topbar-more").click()
    expect(page.locator("#top-menu")).to_be_visible()
    expect(page.locator("#top-menu").get_by_role("menuitem", name="Rename chat", exact=True)).to_have_count(0)
    expect(page.locator("#top-menu").get_by_role("menuitem", name="Local context", exact=True)).to_be_visible()
    page.keyboard.press("Escape")
    row = page.locator(".session-item").filter(has=page.get_by_role("button", name="Beta", exact=True))
    row.hover()
    row.locator(".session-more").click()
    page.locator("#sess-menu").get_by_role("menuitem", name="Rename", exact=True).click()
    sidebar_editor = page.locator("#session-list .rename-input")
    sidebar_editor.fill("Other chat name")
    sidebar_editor.press("Enter")
    expect(page.get_by_role("button", name="Other chat name", exact=True)).to_be_visible()
    expect(page.locator("#sess-title")).to_contain_text("Alpha")
    assert page.evaluate("CodeyUiState.current().active_id") == "a"


def test_saved_title_immediately_updates_search_filter(page):
    page.locator("#chat-search-toggle").click()
    search = page.locator("#chat-search")
    search.fill("new name")
    expect(page.locator("#search-empty")).to_be_visible()
    editor = start_rename(page)
    editor.fill("New name")
    editor.press("Enter")
    expect(page.locator("#search-empty")).to_be_hidden()
    expect(page.locator(".session-title")).to_have_text("New name")


def test_rename_preserves_message_dom_selection_and_reading_offset(page):
    page.evaluate("addToSession('a',{type:'asst',text:'Selectable text.\\n'.repeat(80)})")
    page.locator("#chat-area").evaluate("e => e.scrollTop=120")
    page.locator(".msg.asst .body p").first.evaluate("""e => {
        window.keptMessageBody=e;
        const range=document.createRange(); range.setStart(e.firstChild,0); range.setEnd(e.firstChild,10);
        const selection=getSelection(); selection.removeAllRanges(); selection.addRange(range);
    }""")
    editor = start_rename(page)
    editor.fill("Reading name")
    # Selecting an input naturally owns selection; capture a body range after editing starts.
    page.evaluate("""() => {
        const range=document.createRange(); range.selectNodeContents(keptMessageBody);
        const selection=getSelection(); selection.removeAllRanges(); selection.addRange(range);
    }""")
    selected = page.evaluate("getSelection().toString()")
    offset = page.locator("#chat-area").evaluate("e => e.scrollTop")
    editor.press("Enter")
    assert page.locator(".msg.asst .body p").first.evaluate("e => e===keptMessageBody")
    assert page.locator("#chat-area").evaluate("e => e.scrollTop") == offset
    assert page.evaluate("getSelection().toString()") == selected


def test_first_message_does_not_replace_active_or_confirmed_custom_title(page):
    page.evaluate("CodeyUiState.current().sessions[0].title='New chat'; renderSidebar(); renderChat();")
    editor = start_rename(page)
    editor.fill("My chosen name")
    page.evaluate("addToSession('a',{type:'user',text:'Automatic title source'})")
    expect(editor).to_have_value("My chosen name")
    expect(page.locator(".session-item.active .session-title")).to_have_text("New chat")
    editor.press("Enter")
    editor = start_rename(page)
    editor.fill("New chat")
    editor.press("Enter")
    # Exercise the same normalization used on reload before receiving another user message.
    page.evaluate("""() => {
        const state=JSON.parse(JSON.stringify(CodeyUiState.current()));
        CodeyUiState.apply(state); renderSidebar(); renderChat();
        addToSession('a',{type:'user',text:'Must not override a manually chosen title'});
    }""")
    expect(title_button(page)).to_have_text("New chat")
    expect(page.locator(".session-item.active .session-title")).to_have_text("New chat")


def test_title_renamed_marker_survives_durable_store_without_coercing_untrusted_values(tmp_path):
    store = UiStateStore(tmp_path)
    state = {"active_id": "a", "sessions": [
        {"id": "a", "title": "New chat", "titleRenamed": True},
        {"id": "b", "title": "New chat", "titleRenamed": "true"},
    ], "projects": []}
    store.save(state, base_revision=0)
    restored = store.load()
    assert restored["sessions"][0].get("titleRenamed") is True
    assert not restored["sessions"][1].get("titleRenamed", False)
