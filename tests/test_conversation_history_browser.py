import threading

from playwright.sync_api import expect, sync_playwright

from codey.app import server
from codey.app.context import AppContext


def test_packaged_history_prepends_without_remounting_visible_messages(tmp_path, monkeypatch):
    ctx = AppContext(tmp_path / "state")
    ctx.durable.create_conversation("history")
    latest_user_id = ""
    for index in range(200):
        latest_user_id = ctx.durable.append_message(
            "history", "user_message", {"text": f"Request {index}"}
        ).entry_id
        ctx.durable.append_message(
            "history",
            "assistant_message",
            {"text": f"Answer {index}\n\n" + "Readable paragraph. " * 10},
        )
    ctx.save_ui_state(
        {
            "active_id": "history",
            "projects": [],
            "sessions": [{"id": "history", "title": "Long history"}],
        },
        base_revision=0,
    )
    monkeypatch.setattr(server, "STATE", ctx)
    httpd = server.CodeyHTTPServer(("127.0.0.1", 0), server.Handler)
    worker = threading.Thread(target=httpd.serve_forever, daemon=True)
    worker.start()
    try:
        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=True)
            page = browser.new_page(viewport={"width": 1440, "height": 1000})
            page.goto(httpd.launch_url(f"http://127.0.0.1:{httpd.server_port}/"))
            expect(page.locator(f'.msg.user[data-message-id="{latest_user_id}"]')).to_be_attached()
            expect(page.locator(".msg.user")).to_have_count(50)
            anchor = page.locator(f'.msg.user[data-message-id="{latest_user_id}"]').element_handle()
            page.get_by_role("button", name="Load earlier messages", exact=True).click()
            expect(page.locator(".msg.user")).to_have_count(100)
            assert anchor.evaluate("el => el.isConnected")
            page.get_by_role("button", name="Show all for find", exact=True).click()
            expect(page.locator(".msg.user")).to_have_count(200)
            assert anchor.evaluate("el => el.isConnected")
            browser.close()
    finally:
        httpd.shutdown()
        httpd.server_close()
        worker.join(timeout=5)
        ctx.close()
