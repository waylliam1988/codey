"""Real Codey HTTP/browser coverage for the current A-F UI changes.

These checks deliberately use the packaged application and its real HTTP
routes.  A deterministic local peer is used where a model response is needed;
no external provider is contacted.
"""
from __future__ import annotations

import base64
import json
import sys
import threading
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
from playwright.sync_api import expect, sync_playwright

from codey.app import server
from codey.app.context import AppContext
from codey.app.shell_approval_state import restore_pending_shell_approvals
from tests.support.shell_approval import seed_requested_approval
from tests.test_conversation_live_browser import open_live, seed, send
from tests.test_conversation_question_http import question_peer
from tools.local_model_gate_attempts import GateTarget
from tools.local_model_gate_shell import gate_application


@contextmanager
def failing_peer(status: int = 401):
    """A deterministic OpenAI-compatible peer that returns one real HTTP failure."""
    requests: list[dict] = []
    errors: list[str] = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_GET(self):
            body = json.dumps({"data": [{"id": "failure-fixture"}]}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_POST(self):
            try:
                raw = self.rfile.read(int(self.headers.get("Content-Length", "0")))
                requests.append(json.loads(raw))
                body = json.dumps({"error": {"message": "fixture authentication failed", "type": "invalid_api_key"}}).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            except Exception as exc:
                errors.append(str(exc))

    httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{httpd.server_port}/v1", requests, errors
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=5)


def test_real_provider_failure_exposes_structured_connection_recovery(tmp_path):
    with failing_peer() as (url, requests, peer_errors):
        target = GateTarget(url, "failure-fixture", 32768, 8192, 12000, api_protocol="openai-completions", request_timeout=10)
        with gate_application(target, tmp_path / "gate") as (httpd, ctx, events), sync_playwright() as pw:
            seed(ctx, tmp_path, url, target.model)
            browser, page, errors = open_live(pw, httpd)
            try:
                send(page, "请连接模型并返回一段说明。")
                expect(page.locator(".status-row.err")).to_contain_text("provider failed", timeout=15000)
                expect(page.get_by_role("button", name="Connection settings", exact=True)).to_be_visible()
                assert len(requests) == 1
                assert not peer_errors and not errors
            finally:
                browser.close()


def test_real_http_state_supports_final_copy_and_shared_rich_blocks(tmp_path, monkeypatch):
    project = tmp_path / "project"
    project.mkdir()
    ctx = AppContext(tmp_path / "state")
    payload = {
            "active_id": "rich",
            "projects": [{"id": "p", "name": "Project", "path": str(project)}],
            "sessions": [{
                "id": "rich",
                "title": "Rich response",
                "projectId": "p",
                "messages": [
                    {"type": "user", "id": "u1", "text": "request", "turnId": "turn-1"},
                    {"type": "tool", "kind": "read", "path": "x.py", "result": "tool output", "turnId": "turn-1"},
                    {
                        "type": "asst", "id": "a1", "text": "前置过程，不应进入最终复制。",
                        "runId": "run-1", "turnId": "turn-1", "contentRole": "progress", "answerId": "progress",
                        "answerPartIndex": 0,
                    },
                    {
                        "type": "asst", "id": "a2", "text": "最终第一段\n\n\\(x^2\\)\n\n```mermaid\ngraph TD\n A-->B\n```",
                        "runId": "run-1", "turnId": "turn-1", "contentRole": "final", "finalAnswer": True,
                        "answerId": "answer-1", "answerPartIndex": 0,
                    },
                    {
                        "type": "asst", "id": "a3", "text": "最终第二段 | 保持原文",
                        "runId": "run-1", "turnId": "turn-1", "contentRole": "final", "finalAnswer": True,
                        "answerId": "answer-1", "answerPartIndex": 1,
                    },
                ],
            }],
        }
    ctx.save_ui_state(payload, base_revision=0)
    ctx.durable.create_conversation("rich")
    for message in payload["sessions"][0]["messages"]:
        kind = {"user": "user_message", "asst": "assistant_message"}.get(message["type"], "display_message")
        ctx.durable.append_message("rich", kind, message)
    monkeypatch.setattr(server, "STATE", ctx)
    httpd = server.CodeyHTTPServer(("127.0.0.1", 0), server.Handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=True)
            page = browser.new_page(viewport={"width": 1440, "height": 1000})
            page.goto(httpd.launch_url(f"http://127.0.0.1:{httpd.server_port}/"))
            expect(page.locator(".msg.asst")).to_have_count(3)
            expect(page.locator("math")).to_be_visible()
            expect(page.locator("svg.md-mermaid-svg")).to_be_visible()
            final = page.get_by_role("button", name="Copy final answer", exact=True)
            expect(final).to_have_count(2)
            copy_button = page.locator(".msg.asst").nth(1).locator(".msg-copy")
            page.evaluate("""() => Object.defineProperty(navigator, 'clipboard', {configurable: true, value: {writeText: t => { window.__copied = t; return Promise.resolve(); }}})""")
            copy_button.locator("..").hover()
            copy_button.click(force=True)
            expect(copy_button).to_have_attribute("aria-label", "Copied")
            assert page.evaluate("window.__copied") == "最终第一段\n\n\\(x^2\\)\n\n```mermaid\ngraph TD\n A-->B\n```\n\n最终第二段 | 保持原文"
            browser.close()
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=5)
        ctx.close()


def test_real_http_history_search_returns_archived_context(tmp_path, monkeypatch):
    ctx = AppContext(tmp_path / "state")
    ctx.durable.create_conversation("history")
    messages = []
    needle_id = ""
    for i in range(180):
        text = "needle in archived history" if i == 23 else f"request {i}"
        entry = ctx.durable.append_message("history", "user_message", {"text": text})
        messages.append({"type": "user", "id": entry.entry_id, "text": text})
        if i == 23:
            needle_id = entry.entry_id
    ctx.save_ui_state({"active_id": "history", "projects": [], "sessions": [{"id": "history", "messages": messages}]}, base_revision=0)
    monkeypatch.setattr(server, "STATE", ctx)
    httpd = server.CodeyHTTPServer(("127.0.0.1", 0), server.Handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=True)
            page = browser.new_page(viewport={"width": 1440, "height": 1000})
            page.goto(httpd.launch_url(f"http://127.0.0.1:{httpd.server_port}/"))
            expect(page.locator(".msg.user")).to_have_count(100)
            page.get_by_role("button", name="Search this conversation", exact=True).click()
            page.get_by_role("searchbox", name="Search this conversation", exact=True).fill("needle")
            page.get_by_role("searchbox", name="Search this conversation", exact=True).press("Enter")
            expect(page.locator(".history-search-status")).to_have_text("1 matches")
            page.get_by_role("button", name="Load earlier messages", exact=True).click()
            expect(page.locator(f'.msg.user[data-message-id="{needle_id}"]')).to_be_attached()
            browser.close()
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=5)
        ctx.close()


@pytest.mark.parametrize("protocol", ["openai-completions", "openai-responses"])
def test_real_question_draft_survives_browser_refresh_before_submission(tmp_path, protocol):
    with question_peer(protocol) as (url, requests, peer_errors):
        target = GateTarget(url, "question-fixture", 32768, 8192, 12000, api_protocol=protocol, request_timeout=15)
        with gate_application(target, tmp_path / "gate") as (httpd, ctx, events), sync_playwright() as pw:
            seed(ctx, tmp_path, url, target.model)
            browser, page, errors = open_live(pw, httpd)
            try:
                send(page, "先问我检查范围，再只读分析。")
                card = page.locator(".question-card")
                expect(card).to_be_visible()
                card.get_by_role("radio", name="只读文档", exact=True).check()
                page.reload()
                expect(page.locator(".question-card")).to_be_visible()
                expect(page.locator(".question-card").get_by_role("radio", name="只读文档", exact=True)).to_be_checked()
                page.locator(".question-card").get_by_role("button", name="Confirm", exact=True).click()
                expect(page.locator(".msg.asst")).to_contain_text("已完成只读文档检查", timeout=15000)
                assert len(requests) == 3 and not peer_errors and not errors
            finally:
                browser.close()


def test_real_shell_card_shows_flushed_output_before_process_exit(tmp_path, monkeypatch):
    script = "import sys,time; print('EARLY_OUTPUT', flush=True); time.sleep(6); print('LATE_OUTPUT', flush=True)"
    command = f'"{sys.executable}" -c "import base64;exec(base64.b64decode(\'{base64.b64encode(script.encode()).decode()}\'))"'
    ctx = AppContext(tmp_path / "state")
    seed_requested_approval(ctx, continue_after=False, command=command)
    restore_pending_shell_approvals(ctx)
    ctx.save_ui_state({"active_id": "session", "projects": [], "sessions": [{"id": "session", "title": "Shell", "provider": "local", "messages": []}]}, base_revision=0)
    monkeypatch.setattr(server, "STATE", ctx)
    httpd = server.CodeyHTTPServer(("127.0.0.1", 0), server.Handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=True)
            page = browser.new_page(viewport={"width": 1440, "height": 1000})
            page.goto(httpd.launch_url(f"http://127.0.0.1:{httpd.server_port}/"))
            page.get_by_role("button", name="Allow once", exact=True).click()
            card = page.locator(".command-card")
            expect(card).to_be_visible()
            card.locator("summary").click()
            expect(card.locator(".shell-stdout")).to_contain_text("EARLY_OUTPUT", timeout=1500)
            assert card.locator(".command-label").text_content() == "Running"
            expect(card.locator(".shell-stdout")).to_contain_text("LATE_OUTPUT", timeout=10000)
            expect(card.locator(".command-label")).to_have_text("Ran", timeout=15000)
            browser.close()
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=5)
        ctx.close()
