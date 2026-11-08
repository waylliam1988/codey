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
    monkeypatch.setattr(server.CodeyHTTPServer, "app_context", ctx)
    httpd = server.CodeyHTTPServer(("127.0.0.1", 0), server.Handler)
    worker = threading.Thread(target=httpd.serve_forever, daemon=True)
    worker.start()
    try:
        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=True)
            page = browser.new_page(viewport={"width": 1440, "height": 1000})
            # This fixture exercises real history HTTP, not the separate SSE feed.
            page.add_init_script("window.EventSource = class { static OPEN = 1; readyState = 1; close() {} };")
            page_errors = []
            page.on('pageerror', lambda error: page_errors.append(str(error)))
            older_requests = []

            def append_while_loading(route):
                if 'before=' in route.request.url:
                    older_requests.append(route.request.url)
                    ctx.durable.append_message('history', 'assistant_message', {'text': 'A live update while reading history.'})
                route.continue_()

            page.route('**/api/sessions/history/messages?*', append_while_loading)
            page.goto(httpd.launch_url(f"http://127.0.0.1:{httpd.server_port}/"))
            expect(page.locator(f'.msg.user[data-message-id="{latest_user_id}"]')).to_be_attached()
            expect(page.locator(".msg.user")).to_have_count(50)
            expect(page.locator('.conversation-tick')).to_have_count(200)
            anchor = page.locator(f'.msg.user[data-message-id="{latest_user_id}"]').element_handle()
            assert page.get_by_role('button', name='Load earlier messages', exact=True).count() == 0
            assert page.get_by_role('button', name='Show all for find', exact=True).count() == 0
            assert page.get_by_role('button', name='Search this conversation', exact=True).count() == 0
            area = page.locator('#chat-area')
            area.hover()
            page.mouse.wheel(0, -100000)
            expect(page.locator(".msg.user")).to_have_count(100)
            assert anchor.evaluate("el => el.isConnected")
            for expected in (150, 200):
                area.hover()
                page.mouse.wheel(0, -100000)
                expect(page.locator('.msg.user')).to_have_count(expected)
            assert anchor.evaluate("el => el.isConnected")
            page.keyboard.press('Control+f')
            expect(page.get_by_role('searchbox', name='Search conversation history')).to_be_visible()
            assert len(older_requests) == 3
            page.reload()
            expected_users = sum(item['type'] == 'user' for item in ctx.durable.history_page('history')['items'])
            expect(page.locator('.msg.user')).to_have_count(expected_users)
            expect(page.locator('.conversation-tick')).to_have_count(200)
            head = ctx.durable.store.head('history')
            first_tick = page.locator('.conversation-tick').first
            first_tick.hover()
            expect(page.locator('.conversation-preview-body')).to_contain_text('Answer 0')
            first_tick.click()
            expect(page.locator('#chat > .msg.user').first).to_contain_text('Request 0')
            expect(first_tick).to_have_attribute('aria-current', 'location')
            expect(page.locator('#back-to-latest')).to_be_visible()
            assert ctx.durable.store.head('history') == head
            assert not page_errors
            browser.close()
    finally:
        httpd.shutdown()
        httpd.server_close()
        worker.join(timeout=5)
        ctx.close()


def test_real_history_http_validates_unseen_sse_tasks_and_rechecks_after_path_rewrite(tmp_path, monkeypatch):
    ctx = AppContext(tmp_path / "state", start_scheduler=False)
    for run in ("prefix", "excluded"):
        ctx.durable.admit_input(conversation_id="chat", request_id=run, task_id=run, text=f"Question {run}", provider_snapshot={})
        ctx.durable.complete_task(run, f"Answer {run}", state={})
        if run == "prefix":
            prefix_tip = ctx.durable.store.head("chat")[0]
    ctx.durable.branch("chat", prefix_tip)
    ctx.save_ui_state({"active_id": "chat", "projects": [], "sessions": [{"id": "chat", "title": "Visibility"}]},
                      base_revision=0)
    monkeypatch.setattr(server.CodeyHTTPServer, "app_context", ctx)
    httpd = server.CodeyHTTPServer(("127.0.0.1", 0), server.Handler)
    worker = threading.Thread(target=httpd.serve_forever, daemon=True)
    worker.start()
    try:
        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=True)
            page = browser.new_page()
            page.add_init_script("window.__TEST_BRIDGE__ = true; window.EventSource = class { static OPEN = 1; readyState = 1; close() {} };")
            errors = []
            page.on("pageerror", lambda error: errors.append(str(error)))
            page.goto(httpd.launch_url(f"http://127.0.0.1:{httpd.server_port}/"))
            expect(page.locator("#chat")).to_contain_text("Answer prefix")
            assert not page.evaluate("sessions.find(session => session.id === 'chat').messages.some(message => message.runId === 'excluded')")
            ctx.durable.admit_input(conversation_id="chat", request_id="new", task_id="new", text="New question", provider_snapshot={})
            ctx.durable.complete_task("new", "New answer", state={})
            with page.expect_response("**/api/history/validate_runs") as validation:
                page.evaluate("""() => {
                  handleServerEvent({type:'reply',session_id:'chat',run_id:'excluded',text:'LATE EXCLUDED ANSWER'});
                  handleServerEvent({type:'reply',session_id:'chat',run_id:'new',text:'CONFIRMED NEW ANSWER'});
                }""")
            data = validation.value.json()
            assert {row["run_id"]: row["visible"] for row in data["runs"]} == {"excluded": False, "new": True}
            expect(page.locator("#chat")).to_contain_text("CONFIRMED NEW ANSWER")
            expect(page.locator("#chat")).not_to_contain_text("LATE EXCLUDED ANSWER")
            rewritten = ctx.durable.branch("chat", prefix_tip)
            with page.expect_response("**/api/history/validate_runs") as revalidation:
                page.evaluate("event => ConversationUI.observeCommit(event)", rewritten.record.to_payload())
            assert revalidation.value.json()["runs"] == [{"run_id": "new", "visible": False}]
            expect(page.locator("#chat")).not_to_contain_text("CONFIRMED NEW ANSWER")
            page.evaluate("handleServerEvent({type:'reply',session_id:'chat',run_id:'new',text:'AFTER REWRITE'})")
            expect(page.locator("#chat")).not_to_contain_text("AFTER REWRITE")
            assert not errors
            browser.close()
    finally:
        httpd.shutdown()
        httpd.server_close()
        worker.join(5)
        ctx.close()
