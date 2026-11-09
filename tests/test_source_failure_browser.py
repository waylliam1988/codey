"""English source failures survive production history and native disclosure."""
from __future__ import annotations

import threading
from pathlib import Path

import pytest
from playwright.sync_api import expect, sync_playwright

from codey.app import server
from codey.app.context import AppContext
from codey.research.source_acquisition import failure_event
from tests.http_context import attach_public_events


def test_source_failure_history_disclosure_and_responsive_layout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    state = tmp_path / "state"
    ctx = AppContext(state, start_scheduler=False)
    ctx.durable.admit_input(conversation_id="chat", request_id="one", task_id="r", text="Read sources", provider_snapshot={})
    reasons = [("access_restricted", "Access restricted"), ("page_not_loaded", "Page not loaded"),
               ("render_script_error", "Page script failed")]
    for index, (code, _) in enumerate(reasons):
        url = f"https://example.com/{index}"
        ctx.emit({"schema_version": 1, "type": "tool", "run_id": "r", "session_id": "chat",
                  "turn": index + 1, "tool_id": f"{index + 1}:0", "call_id": f"{index + 1}:0",
                  "tool": "read", "tool_name": "open_url", "path": url, "ok": False, "status": "error",
                  "result": "Observed diagnostic: HTTP 403 and page_blank",
                  "failure": failure_event({"url": url, "detail": "Observed diagnostic", "code": code},
                      run_id="r", session_id="chat", call_id=f"{index + 1}:0")})
    ctx.save_ui_state({"active_id": "chat", "projects": [], "sessions": [
        {"id": "chat", "title": "Source diagnostics"}]}, base_revision=0)
    ctx.close()
    ctx = AppContext(state, start_scheduler=False)
    saved = [row for row in ctx.durable.history_page("chat")["items"] if row.get("type") == "tool"]
    assert [row["failure"]["code"] for row in saved] == ["source." + code for code, _ in reasons]
    monkeypatch.setattr(server.CodeyHTTPServer, "app_context", ctx)
    httpd = server.CodeyHTTPServer(("127.0.0.1", 0), server.Handler)
    close_events = attach_public_events(httpd)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    output = Path(".e2e-artifacts/browser-research-convergence-20261109/ui")
    output.mkdir(parents=True, exist_ok=True)
    try:
        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=True)
            page = browser.new_page(viewport={"width": 1440, "height": 900})
            page.set_default_timeout(10000)
            errors: list[str] = []
            page.on("pageerror", lambda error: errors.append(str(error)))
            page.goto(httpd.launch_url(f"http://127.0.0.1:{httpd.server_port}/"))
            expect(page.locator(".tool-disclosure")).to_have_count(3)
            for group in page.locator(".process-group:not([open]) > summary").all():
                group.click()
            for index, (_, label) in enumerate(reasons):
                row = page.locator(".tool-disclosure").nth(index)
                expect(row.locator(".tl-result")).to_contain_text(label)
                expect(row.locator(".tl-path")).to_have_text(f"https://example.com/{index}")
                row.locator("summary").click()
                expect(row.locator(".tool-result-panel")).to_contain_text("Observed diagnostic: HTTP 403 and page_blank")
                row.locator("summary").click()
            for width in (1440, 600):
                page.set_viewport_size({"width": width, "height": 900})
                assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
                page.screenshot(path=str(output / f"source-failures-{width}.png"))
            page.reload()
            for group in page.locator(".process-group:not([open]) > summary").all():
                group.click()
            expect(page.locator(".tl-result").last).to_contain_text("Page script failed")
            assert not errors
            browser.close()
    finally:
        close_events()
        httpd.shutdown()
        httpd.server_close()
        thread.join(5)
        ctx.close()
