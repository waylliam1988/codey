"""Sidebar sizing and copying an exact selection through the shipped UI."""
from __future__ import annotations

import pytest
from playwright.sync_api import expect

from tests.test_ui_workflow import page as page
from tests.test_ui_workflow import ui_browser as ui_browser


def expect_width(page, value):
    page.wait_for_function("value => Math.abs(document.getElementById('aside').getBoundingClientRect().width-value)<1", arg=value)


def drag_sidebar(page, delta, *, release=True):
    handle = page.get_by_role("separator", name="Resize sidebar", exact=True)
    expect(handle).to_be_visible()
    box = handle.bounding_box()
    x, y = box["x"] + box["width"] / 2, box["y"] + 160
    page.mouse.move(x, y)
    page.mouse.down()
    page.mouse.move(x + delta, y, steps=12)
    if release:
        page.mouse.up()
    return handle


def test_sidebar_drag_preserves_draft_and_stays_neutral(page):
    page.locator("#task").fill("keep this draft")
    handle = drag_sidebar(page, 90)
    expect_width(page, 350)
    assert page.evaluate("JSON.parse(localStorage.getItem('codey:sidebar-width'))") == 350
    expect(page.locator("#task")).to_have_value("keep this draft")
    expect(handle).to_have_attribute("aria-valuenow", "350")
    assert handle.evaluate("e => getComputedStyle(e).cursor") == "col-resize"
    assert handle.evaluate("e => getComputedStyle(e).backgroundColor") == "rgba(0, 0, 0, 0)"
    assert page.locator(".chat-inner").bounding_box()["width"] <= 760


def test_sidebar_width_survives_reload_collapse_and_double_click_reset(page):
    drag_sidebar(page, 100)
    page.reload()
    expect_width(page, 360)
    page.locator("#toggle-sidebar").click()
    expect_width(page, 0)
    expect(page.get_by_role("separator", name="Resize sidebar")).to_be_hidden()
    page.locator("#show-sidebar").click()
    expect_width(page, 360)
    page.get_by_role("separator", name="Resize sidebar").dblclick()
    expect_width(page, 260)
    assert page.evaluate("JSON.parse(localStorage.getItem('codey:sidebar-width'))") == 260


def test_sidebar_limits_keyboard_and_narrow_overlay_preserve_preference(page):
    handle = drag_sidebar(page, 500)
    expect_width(page, 420)
    handle.focus()
    handle.press("Home")
    expect_width(page, 220)
    handle.press("ArrowRight")
    expect_width(page, 230)
    handle.press("End")
    expect_width(page, 420)
    page.set_viewport_size({"width": 780, "height": 800})
    expect_width(page, 380)
    assert page.locator("main").bounding_box()["width"] >= 400
    page.set_viewport_size({"width": 480, "height": 800})
    expect(handle).to_be_hidden()
    page.locator("#show-sidebar").click()
    expect_width(page, 260)
    assert page.evaluate("JSON.parse(localStorage.getItem('codey:sidebar-width'))") == 420
    page.set_viewport_size({"width": 1280, "height": 800})
    expect_width(page, 420)


@pytest.mark.parametrize(("stored", "expected"), [("broken", 260), ("9999", 420), ("-40", 220)])
def test_sidebar_restores_only_bounded_valid_preferences(page, stored, expected):
    page.evaluate("value => localStorage.setItem('codey:sidebar-width', value)", stored)
    page.reload()
    expect(page.get_by_role("separator", name="Resize sidebar")).to_be_visible()
    expect_width(page, expected)


def test_sidebar_escape_cancels_drag_and_storage_failure_does_not_break_ui(page):
    drag_sidebar(page, 90)
    drag_sidebar(page, 40, release=False)
    page.keyboard.press("Escape")
    page.mouse.up()
    expect_width(page, 350)
    assert page.evaluate("JSON.parse(localStorage.getItem('codey:sidebar-width'))") == 350
    page.evaluate("() => {Storage.prototype.setItem = () => {throw new Error('unavailable')}}")
    drag_sidebar(page, 10)
    expect_width(page, 360)
    assert not page.locator("body").evaluate("e => e.classList.contains('sidebar-resizing')")


def stub_clipboard(page, *, succeeds=True):
    page.evaluate("""succeeds => {
        window.copiedSelections = [];
        CodeyRender.copyText = async text => { copiedSelections.push(text); return succeeds; };
    }""", succeeds)


def select_message(page, kind="asst", text="Before SELECTED 中文 after"):
    page.evaluate("([kind,text]) => addToSession('a',{type:kind,text})", [kind, text])
    body = page.locator(f".msg.{kind} .body").last
    body.evaluate("""element => {
        const node = element.querySelector('p')?.firstChild || element.firstChild;
        const range = document.createRange(); range.setStart(node,7); range.setEnd(node,18);
        const selection = getSelection(); selection.removeAllRanges(); selection.addRange(range);
    }""")
    return body


def open_selection_menu(page, target):
    # Dispatching preserves a precise range; native review also uses real right clicks.
    right_click(target, 650, 190)
    menu = page.get_by_role("menu", name="Selection", exact=True)
    expect(menu).to_be_visible()
    return menu


def right_click(target, x=650, y=190):
    target.evaluate("""(element, [x,y]) => element.dispatchEvent(new MouseEvent('contextmenu', {
        clientX:x,clientY:y,bubbles:true,cancelable:true,button:2,
    }))""", [x, y])


@pytest.mark.parametrize("kind", ["user", "asst"])
def test_message_context_copy_uses_only_selection_and_retains_gray_menu(page, kind):
    stub_clipboard(page)
    body = select_message(page, kind)
    selected = page.evaluate("getSelection().toString()")
    assert selected == "SELECTED 中文"
    menu = open_selection_menu(page, body)
    copy = menu.get_by_role("menuitem", name="Copy", exact=True)
    expect(copy).to_be_focused()
    assert menu.evaluate("e => getComputedStyle(e).backgroundColor") == "rgb(32, 32, 32)"
    assert copy.evaluate("e => getComputedStyle(e).color") == "rgb(160, 160, 160)"
    copy.click()
    page.wait_for_function("copiedSelections.length === 1")
    assert page.evaluate("copiedSelections") == [selected]
    expect(menu).to_be_hidden()
    assert page.evaluate("getSelection().toString()") == selected


def test_composer_context_copy_preserves_text_caret_and_draft(page):
    stub_clipboard(page)
    task = page.locator("#task")
    task.fill("before SELECTED after")
    task.evaluate("e => e.setSelectionRange(7,15)")
    menu = open_selection_menu(page, task)
    menu.get_by_role("menuitem", name="Copy", exact=True).click()
    expect(menu).to_be_hidden()
    assert page.evaluate("copiedSelections") == ["SELECTED"]
    expect(task).to_have_value("before SELECTED after")
    expect(task).to_be_focused()
    assert task.evaluate("e => [e.selectionStart,e.selectionEnd]") == [7, 15]
    page.evaluate("switchSession('b'); switchSession('a')")
    expect(task).to_have_value("before SELECTED after")


def test_context_copy_menu_has_plain_rows_for_pointer_and_keyboard(page):
    task = page.locator("#task")
    task.fill("before SELECTED after")
    task.evaluate("e => e.setSelectionRange(7,15)")
    menu = open_selection_menu(page, task)
    copy = menu.get_by_role("menuitem", name="Copy", exact=True)
    assert copy.evaluate("e => getComputedStyle(e).outlineWidth") == "0px"
    assert copy.evaluate("e => getComputedStyle(e).borderTopWidth") == "0px"
    assert copy.evaluate("e => getComputedStyle(e).backgroundColor") == "rgba(0, 0, 0, 0)"
    copy.hover()
    assert copy.evaluate("e => getComputedStyle(e).backgroundColor") == "rgb(42, 42, 42)"
    page.keyboard.press("Escape")
    page.mouse.move(20,20)
    task.press("Shift+F10")
    expect(menu).to_be_visible()
    expect(copy).to_be_focused()
    assert copy.evaluate("e => getComputedStyle(e).outlineWidth") == "0px"
    assert copy.evaluate("e => getComputedStyle(e).backgroundColor") == "rgb(42, 42, 42)"


def test_selection_menu_keyboard_escape_and_chat_switch_close_stale_menu(page):
    stub_clipboard(page)
    task = page.locator("#task")
    task.fill("before SELECTED after")
    task.evaluate("e => e.setSelectionRange(7,15)")
    task.press("Shift+F10")
    menu = page.get_by_role("menu", name="Selection", exact=True)
    expect(menu).to_be_visible()
    page.keyboard.press("Escape")
    expect(menu).to_be_hidden()
    expect(task).to_be_focused()
    assert task.evaluate("e => [e.selectionStart,e.selectionEnd]") == [7,15]
    task.press("Shift+F10")
    page.keyboard.press("Enter")
    expect(menu).to_be_hidden()
    assert page.evaluate("copiedSelections") == ["SELECTED"]
    body = select_message(page)
    open_selection_menu(page, body)
    page.evaluate("switchSession('b')")
    expect(menu).to_be_hidden()


def test_selection_copy_failure_keeps_retryable_menu_and_selected_snapshot(page):
    stub_clipboard(page, succeeds=False)
    body = select_message(page)
    selected = page.evaluate("getSelection().toString()")
    menu = open_selection_menu(page, body)
    copy = menu.get_by_role("menuitem", name="Copy", exact=True)
    copy.click()
    expect(menu.get_by_role("status")).to_have_text("Could not copy")
    expect(copy).to_be_enabled()
    page.evaluate("() => {CodeyRender.copyText = async text => {copiedSelections.push(text); return true}}")
    copy.click()
    expect(menu).to_be_hidden()
    assert page.evaluate("copiedSelections") == [selected, selected]


def test_selection_menu_is_bounded_and_dismissed_on_scroll_and_outside_click(page):
    body = select_message(page)
    right_click(body, 1277, 797)
    menu = page.get_by_role("menu", name="Selection", exact=True)
    expect(menu).to_be_visible()
    box = menu.bounding_box()
    assert box["x"] >= 8 and box["x"] + box["width"] <= 1272
    assert box["y"] >= 8 and box["y"] + box["height"] <= 792
    page.locator("#chat-area").dispatch_event("scroll")
    expect(menu).to_be_hidden()
    open_selection_menu(page, body)
    page.locator("#task").click()
    expect(menu).to_be_hidden()


def test_context_copy_never_uses_stale_selection_for_unselected_input_or_password(page):
    body = select_message(page)
    open_selection_menu(page, body)
    task = page.locator("#task")
    task.fill("no selection")
    task.evaluate("e => e.setSelectionRange(0,0)")
    right_click(task, 650, 720)
    expect(page.get_by_role("menu", name="Selection", exact=True)).to_be_hidden()
    page.evaluate("""() => {
        const password = document.createElement('input'); password.type='password';
        password.id='private-selection'; password.value='private key'; document.body.append(password);
        password.focus(); password.select();
    }""")
    right_click(page.locator("#private-selection"))
    expect(page.get_by_role("menu", name="Selection", exact=True)).to_be_hidden()
