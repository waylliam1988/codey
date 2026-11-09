"""Real Chromium rich-text copy must exclude collapsed process contents."""
from __future__ import annotations

import threading
from collections.abc import Iterator
from pathlib import Path

import pytest
from playwright.sync_api import Page, expect, sync_playwright

from codey.app import server
from codey.app.context import AppContext
from codey.types.json_value import decode_json_object


@pytest.fixture
def clipboard_page(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Page]:
    ctx = AppContext(tmp_path / "state", start_scheduler=False)
    ctx.durable.admit_input(conversation_id="copy", request_id="one", task_id="run-copy",
                            text="Recommend some food", provider_snapshot={})
    ctx.durable.record_display_message("copy", run_id="run-copy", identity="thoughts", payload={
        "type": "turn", "runId": "run-copy", "n": 1,
        "reasoning": "Notes\n\n" + "\n".join(f"- Hidden food {index}" for index in range(9)),
    })
    ctx.durable.complete_task("run-copy", "Recommendations\n\n- Fish balls\n- Meat dumplings", state={})
    ctx.durable.append_message("copy", "user_message", {"text": "And for dessert?"})
    ctx.durable.append_message("copy", "assistant_message", {"text": "Dessert\n\n- Taro paste"})
    ctx.save_ui_state({"active_id": "copy", "projects": [], "sessions": [
        {"id": "copy", "title": "Clipboard regression"}]}, base_revision=0)
    monkeypatch.setattr(server.CodeyHTTPServer, "app_context", ctx)
    httpd = server.CodeyHTTPServer(("127.0.0.1", 0), server.Handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=True)
            context = browser.new_context(permissions=["clipboard-read", "clipboard-write"])
            page = context.new_page()
            page.add_init_script("window.EventSource = class { static OPEN = 1; readyState = 1; close() {} };")
            page.goto(httpd.launch_url(f"http://127.0.0.1:{httpd.server_port}/"))
            expect(page.locator(".thinking li")).to_have_count(9)
            expect(page.locator(".asst li")).to_have_count(3)
            yield page
            browser.close()
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(5)
        ctx.close()


@pytest.mark.parametrize(("process_open", "thinking_open"), [(False, False), (False, True), (True, False), (True, True)])
def test_rich_text_selection_excludes_collapsed_thoughts(
    clipboard_page: Page, process_open: bool, thinking_open: bool,
) -> None:
    page = clipboard_page
    page.evaluate("""([outer, inner]) => {
        document.querySelector('.process-group').open = outer;
        document.querySelector('.thinking').open = inner;
    }""", [process_open, thinking_open])
    page.locator(".asst .body").last.click(position={"x": 5, "y": 5})
    assert page.locator(".process-group").get_attribute("open") == ("" if process_open else None)
    assert page.locator(".thinking").get_attribute("open") == ("" if thinking_open else None)
    page.keyboard.press("Control+a")
    page.keyboard.press("Control+c")
    copied = decode_json_object(page.evaluate("""async () => {
        let html = '';
        for (const item of await navigator.clipboard.read()) {
            if (item.types.includes('text/html')) html = await (await item.getType('text/html')).text();
        }
        const dom = new DOMParser().parseFromString(html, 'text/html');
        const list = [...dom.querySelectorAll('li')];
        return {text: await navigator.clipboard.readText(),
            items: list.map(item => item.textContent.trim()),
            emptyItems: list.filter(item => !item.textContent.trim()).length};
    }"""))
    assert copied["emptyItems"] == 0
    text = copied["text"]
    assert isinstance(text, str)
    for expected in ("Fish balls", "Meat dumplings", "Taro paste"):
        assert expected in text
    items = copied["items"]
    assert isinstance(items, list)
    visible = process_open and thinking_open
    assert len(items) == (12 if visible else 3)
    assert ("Hidden food 0" in text) == visible
    expect(page.locator(".thinking li")).to_have_count(9)
