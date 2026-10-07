"""Whole-message Copy remains reachable while its resting icon is hidden."""
from __future__ import annotations

import pytest
from playwright.sync_api import expect

from tests.test_ui_workflow import page as page
from tests.test_ui_workflow import ui_browser as ui_browser


def append_message(page, kind="asst", text="A message to copy."):
    page.evaluate("([kind,text]) => addToSession('a',{type:kind,text})", [kind, text])
    messages = page.locator(f".msg.{kind}")
    return messages.nth(messages.count() - 1)


def expect_opacity(button, value):
    expect(button).to_have_css("opacity", str(value))


def stub_message_clipboard(page, mode="success"):
    page.evaluate("""mode => {
        window.messageCopies = [];
        Object.defineProperty(navigator, 'clipboard', {configurable:true, value:{
            writeText: text => {
                messageCopies.push(text);
                if (mode === 'pending') return new Promise(resolve => {window.finishMessageCopy=resolve});
                if (mode === 'failure') return Promise.reject(new Error('clipboard unavailable'));
                return Promise.resolve();
            },
        }});
        if (mode === 'failure') document.execCommand = () => false;
    }""", mode)


@pytest.mark.parametrize("kind", ["user", "asst"])
def test_copy_hidden_then_reachable_across_gap_without_layout_shift(page, kind):
    stub_message_clipboard(page)
    message = append_message(page, kind, "Copy the entire message, including 中文.")
    following = append_message(page, "asst", "Following message stays in place.")
    copy = message.locator(":scope > .msg-copy")
    page.mouse.move(20, 20)
    expect_opacity(copy, 0)
    expect(copy).to_have_css("pointer-events", "none")
    before = following.bounding_box()
    box = copy.bounding_box()
    body = message.locator(".body").bounding_box()
    x = box["x"] + box["width"] / 2
    page.mouse.move(x, body["y"] + body["height"] / 2)
    expect_opacity(copy, 1)
    expect_opacity(following.locator(":scope > .msg-copy"), 0)
    # Stop in the actual 8px path between the text and the button.
    page.mouse.move(x, box["y"] - 4, steps=12)
    expect_opacity(copy, 1)
    page.mouse.move(x, box["y"] + box["height"] / 2, steps=12)
    expect_opacity(copy, 1)
    assert copy.bounding_box() == box
    assert following.bounding_box() == before
    page.mouse.click(x, box["y"] + box["height"] / 2)
    expect(copy).to_have_attribute("aria-label", "Copied")
    assert page.evaluate("messageCopies") == ["Copy the entire message, including 中文."]
    page.mouse.move(20, 20)
    expect_opacity(copy, 1)  # Feedback remains visible after leaving the message.
    expect(copy).to_have_attribute("aria-label", "Copy message")
    assert copy.evaluate("e => document.activeElement === e")
    assert not copy.evaluate("e => e.matches(':focus-visible')")
    expect_opacity(copy, 0)  # Pointer focus must not leave a permanent icon.
    expect(copy).to_have_css("pointer-events", "none")
    assert following.bounding_box() == before


@pytest.mark.parametrize("kind", ["user", "asst"])
def test_keyboard_tab_reveals_copy_and_enter_copies_without_hover(page, kind):
    stub_message_clipboard(page)
    message = append_message(page, kind, "Keyboard copying.")
    copy = message.locator(":scope > .msg-copy")
    message.locator(".body").click()
    page.mouse.move(20, 20)
    expect_opacity(copy, 0)
    page.keyboard.press("Tab")
    expect(copy).to_be_focused()
    assert copy.evaluate("e => e.matches(':focus-visible')")
    expect_opacity(copy, 1)
    page.keyboard.press("Enter")
    expect(copy).to_have_attribute("aria-label", "Copied")
    assert page.evaluate("messageCopies") == ["Keyboard copying."]
    expect(copy).to_have_attribute("aria-label", "Copy message")
    expect_opacity(copy, 1)
    page.keyboard.press("Tab")
    expect_opacity(copy, 0)


def test_pending_copy_survives_mouse_exit_and_rejects_duplicate_clicks(page):
    stub_message_clipboard(page, "pending")
    message = append_message(page)
    copy = message.locator(":scope > .msg-copy")
    message.locator(".body").hover()
    copy.click()
    expect(copy).to_be_disabled()
    page.mouse.move(20, 20)
    expect_opacity(copy, 1)
    copy.evaluate("e => e.click()")
    assert page.evaluate("messageCopies") == ["A message to copy."]
    page.evaluate("finishMessageCopy()")
    expect(copy).to_be_enabled()
    expect(copy).to_have_attribute("aria-label", "Copied")


def test_copy_failure_remains_readable_after_mouse_exit_and_can_retry(page):
    stub_message_clipboard(page, "failure")
    message = append_message(page)
    copy = message.locator(":scope > .msg-copy")
    message.locator(".body").hover()
    copy.click()
    page.mouse.move(20, 20)
    expect(message.get_by_role("status")).to_have_text("Could not copy")
    expect(copy).to_be_enabled()
    expect_opacity(copy, 1)
    assert message.get_by_role("status").evaluate("e => getComputedStyle(e).color") == "rgb(160, 160, 160)"
    stub_message_clipboard(page)
    copy.click()
    expect(copy).to_have_attribute("aria-label", "Copied")
    expect(message.get_by_role("status", include_hidden=True)).to_have_text("")
    assert page.evaluate("messageCopies") == ["A message to copy."]


@pytest.mark.parametrize("keyboard", [False, True])
def test_clipboard_fallback_preserves_keyboard_access_without_sticky_pointer_focus(page, keyboard):
    stub_message_clipboard(page, "failure")
    page.evaluate("() => {document.execCommand = () => true}")
    message = append_message(page)
    copy = message.locator(":scope > .msg-copy")
    if keyboard:
        message.locator(".body").click()
        page.mouse.move(20, 20)
        page.keyboard.press("Tab")
        expect(copy).to_be_focused()
        page.keyboard.press("Enter")
    else:
        message.locator(".body").hover()
        copy.click()
        page.mouse.move(20, 20)
    expect(copy).to_have_attribute("aria-label", "Copied")
    if keyboard:
        expect(copy).to_be_focused()
    expect(copy).to_have_attribute("aria-label", "Copy message")
    expect_opacity(copy, int(keyboard))


@pytest.mark.parametrize("kind", ["user", "asst"])
def test_copy_remains_visible_and_tappable_without_hover(ui_browser, kind):
    browser, url = ui_browser
    touch = browser.new_page(has_touch=True, is_mobile=True, viewport={"width": 480, "height": 800})
    try:
        touch.add_init_script("window.EventSource = class { static OPEN = 1; readyState = 1; close() {} };")
        state = {"active_id": "a", "projects": [], "sessions": [{"id": "a", "title": "Touch", "provider": "deepseek", "messages": []}]}

        def api(route):
            path = route.request.url.split("/api/")[-1].split("?")[0]
            data = {"ok": True}
            if path == "ui_state":
                data = {"state": state}
            elif path in {"provider_catalog", "providers"}:
                data = {"default": "deepseek", "providers": [{"id": "deepseek", "label": "DeepSeek", "available": False}]}
            route.fulfill(json=data)

        touch.route("**/api/**", api)
        touch.goto(url)
        expect(touch.locator("#provider-button")).to_be_enabled(timeout=15000)
        assert touch.evaluate("matchMedia('(hover: none)').matches")
        stub_message_clipboard(touch)
        copy = append_message(touch, kind, "Tap to copy.").locator(":scope > .msg-copy")
        expect_opacity(copy, 1)
        expect(copy).to_have_css("pointer-events", "auto")
        copy.tap()
        expect(copy).to_have_attribute("aria-label", "Copied")
        assert touch.evaluate("messageCopies") == ["Tap to copy."]
    finally:
        touch.close()


def test_whole_message_visibility_does_not_hide_code_or_thinking_copy(page):
    append_message(page, text="```python\nprint('hello')\n```")
    code_copy = page.locator(".code-copy")
    page.mouse.move(20, 20)
    expect_opacity(code_copy, 0)
    page.locator(".md-code").hover()
    expect_opacity(code_copy, 1)
    page.evaluate("addToSession('a',{type:'thinking',runId:'copy-review',text:'Reasoning text.'})")
    page.locator(".thinking > summary").click()
    thinking_copy = page.locator(".thinking-body > .msg-copy")
    page.locator("#task").click()
    page.mouse.move(20, 20)
    expect_opacity(thinking_copy, 0.45)
    page.locator(".thinking-body").hover()
    expect_opacity(thinking_copy, 1)


def test_long_answer_copy_path_preserves_collapse_and_pointer_selection(page):
    text = "A long selectable reply.\n" * 25
    message = append_message(page, text=text)
    message.get_by_role("button", name="Collapse", exact=True).click()
    message.locator(".body").evaluate("""element => {
        const range = document.createRange(); range.selectNodeContents(element.querySelector('p'));
        const selection = getSelection(); selection.removeAllRanges(); selection.addRange(range);
    }""")
    selected = page.evaluate("getSelection().toString()")
    copy = message.locator(":scope > .msg-copy")
    page.mouse.move(20, 20)
    expect_opacity(copy, 0)
    box = copy.bounding_box()
    page.mouse.move(box["x"] + 13, box["y"] - 4)
    expect_opacity(copy, 1)
    assert page.evaluate("getSelection().toString()") == selected
    expect(message.get_by_role("button", name="Expand", exact=True)).to_be_visible()
