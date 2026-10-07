"""Review resizing and selection Copy in a real, isolated WebView2 window."""
from __future__ import annotations

import ctypes
import sys
from pathlib import Path

from playwright.sync_api import expect

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tools.ui_recovery_search_review import main as native_review

ARTIFACTS = Path(__file__).resolve().parents[1] / ".e2e-artifacts/sidebar-copy-2026-10-07"


def clipboard_text():
    user = ctypes.windll.user32
    kernel = ctypes.windll.kernel32
    user.GetClipboardData.restype = ctypes.c_void_p
    kernel.GlobalLock.argtypes = [ctypes.c_void_p]
    kernel.GlobalLock.restype = ctypes.c_void_p
    kernel.GlobalUnlock.argtypes = [ctypes.c_void_p]
    if not user.OpenClipboard(None):
        raise RuntimeError("Clipboard is busy")
    try:
        handle = user.GetClipboardData(13)
        if not handle:
            return None
        pointer = kernel.GlobalLock(handle)
        try:
            return ctypes.wstring_at(pointer) if pointer else None
        finally:
            if pointer:
                kernel.GlobalUnlock(handle)
    finally:
        user.CloseClipboard()


def exercise(page, url, window):
    checks, errors = [], []
    state = {"active_id": "a", "projects": [
        {"id": "project", "name": "alpha-long-project-name-for-review", "path": "E:/review", "expanded": True},
    ], "sessions": [
        {"id": "a", "title": "Selection review", "projectId": "project", "provider": "deepseek", "messages": [
            {"type": "user", "text": "Before SELECTED 中文 after"},
            {"type": "asst", "text": "Before SELECTED 中文 after\n\nOnly the highlighted words should be copied."},
        ]},
        {"id": "b", "title": "Another chat", "provider": "deepseek", "messages": []},
    ]}

    def api(route):
        path = route.request.url.split("/api/")[-1].split("?")[0]
        data = {"ok": True}
        if path in {"provider_catalog", "providers"}:
            data = {"default": "deepseek", "providers": [{"id": "deepseek", "label": "DeepSeek", "available": False}]}
        elif path == "ui_state" and route.request.method == "GET":
            data = {"state": state}
        route.fulfill(json=data)

    def shot(name):
        page.screenshot(path=str(ARTIFACTS / f"{name}.png"))

    def check(message):
        checks.append(message)
        print(message, flush=True)

    def drag(delta):
        handle = page.get_by_role("separator", name="Resize sidebar")
        box = handle.bounding_box()
        x, y = box["x"] + box["width"] / 2, box["y"] + 150
        page.mouse.move(x, y)
        page.mouse.down()
        page.mouse.move(x + delta, y, steps=20)
        page.mouse.up()

    def select_reply(kind):
        body = page.locator(f".msg.{kind} .body")
        rect = body.evaluate("""element => {
            const node=element.querySelector('p')?.firstChild || element.firstChild;
            const r=document.createRange(); r.setStart(node,7); r.setEnd(node,18);
            const b=r.getBoundingClientRect(); return {x:b.left,y:b.top,width:b.width,height:b.height};
        }""")
        y = rect["y"] + rect["height"] / 2
        page.mouse.move(rect["x"] + 0.2, y)
        page.mouse.down()
        page.mouse.move(rect["x"] + rect["width"] - 0.2, y, steps=16)
        page.mouse.up()
        selected = page.evaluate("getSelection().toString()")
        assert "SELECTED" in selected and "after" not in selected, selected
        page.mouse.click(rect["x"] + rect["width"] / 2, y, button="right")
        return selected

    page.on("pageerror", lambda error: errors.append(str(error)))
    page.add_init_script("window.EventSource = class { static OPEN = 1; readyState = 1; close() {} };")
    page.route("**/api/**", api)
    page.goto(url)
    expect(page.locator("#provider-button")).to_be_enabled(timeout=15000)
    expect(page.locator(".msg.asst")).to_be_visible()
    shot("sidebar-default")
    drag(120)
    page.wait_for_function("Math.abs(document.getElementById('aside').getBoundingClientRect().width-380)<1")
    shot("sidebar-wide")
    page.reload()
    page.wait_for_function("Math.abs(document.getElementById('aside').getBoundingClientRect().width-380)<1")
    page.locator("#toggle-sidebar").click()
    page.locator("#show-sidebar").click()
    page.wait_for_function("Math.abs(document.getElementById('aside').getBoundingClientRect().width-380)<1")
    check("Sidebar: real pointer drag reaches 380px; reload and collapse/expand preserve width")

    original = clipboard_text()
    copied = None
    restored = None
    try:
        for kind in ("user", "asst"):
            copied = select_reply(kind)
            menu = page.get_by_role("menu", name="Selection", exact=True)
            expect(menu).to_be_visible()
            shot(f"copy-{kind}")
            menu.get_by_role("menuitem", name="Copy", exact=True).click()
            expect(menu).to_be_hidden()
            assert clipboard_text() == copied
            assert page.evaluate("getSelection().toString()") == copied
            check(f"Copy {kind}: real mouse selection and right click write exactly selected text to Windows clipboard")

        task = page.locator("#task")
        task.fill("before SELECTED after")
        task.press("Home")
        for _ in range(7):
            task.press("ArrowRight")
        page.keyboard.down("Shift")
        for _ in range(8):
            task.press("ArrowRight")
        page.keyboard.up("Shift")
        task.click(button="right", position={"x": 80, "y": 15})
        menu = page.get_by_role("menu", name="Selection", exact=True)
        expect(menu).to_be_visible()
        assert menu.get_by_role('menuitem', name='Copy', exact=True).evaluate('e => getComputedStyle(e).outlineWidth') == '0px'
        shot("copy-composer")
        menu.get_by_role("menuitem", name="Copy", exact=True).click()
        expect(menu).to_be_hidden()
        copied = "SELECTED"
        assert clipboard_text() == copied
        expect(task).to_have_value("before SELECTED after")
        expect(task).to_be_focused()
        assert task.evaluate("e => [e.selectionStart,e.selectionEnd]") == [7,15]
        task.press("Shift+F10")
        expect(menu).to_be_visible()
        shot("copy-keyboard")
        page.keyboard.press("Escape")
        expect(menu).to_be_hidden()
        check("Composer: right-click Copy preserves draft/selection; Shift+F10 and Escape return focus")

        window.resize(780, 700)
        page.wait_for_function("innerWidth <= 780")
        assert page.locator("main").bounding_box()["width"] >= 400
        shot("sidebar-bounded")
        window.resize(500, 540)
        page.wait_for_function("innerWidth <= 500")
        expect(page.get_by_role("separator", name="Resize sidebar")).to_be_hidden()
        page.locator("#show-sidebar").click()
        page.wait_for_function("Math.abs(document.getElementById('aside').getBoundingClientRect().width-260)<1")
        shot("sidebar-narrow")
        assert page.evaluate("JSON.parse(localStorage.getItem('codey:sidebar-width'))") == 380
        check("Sidebar: compact desktop preserves 400px main area; narrow overlay leaves saved width intact")
    finally:
        if original is not None and copied is not None and clipboard_text() == copied:
            restored = page.evaluate("text => CodeyRender.copyText(text)", original)
    assert not errors, errors
    return {"host": "pywebview / Edge WebView2", "isolated_state": True,
            "checks": checks, "javascript_errors": errors, "clipboard_text_restored": restored}


if __name__ == "__main__":
    raise SystemExit(native_review(exercise, ARTIFACTS))
