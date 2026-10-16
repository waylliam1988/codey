"""Production UI -> HTTP -> native provider -> SSE, without injecting UI events."""
from __future__ import annotations

import json
import os
import threading
import time
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest
from playwright.sync_api import expect, sync_playwright

from tests.test_conversation_question_http import question_peer
from tools.local_model_gate_attempts import GateTarget
from tools.local_model_gate_shell import gate_application


@contextmanager
def streaming_peer(protocol, *, hold_all=False, max_requests=2):
    release = threading.Event()
    requests, errors = [], []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_GET(self):
            data = json.dumps({"data": [{"id": "live-fixture"}]}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_POST(self):
            try:
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                requests.append(body)
                assert body["stream"] is True
                assert len(requests) <= max_requests, "unexpected repeated request"
                first = len(requests) == 1
                head = "# 项目检查\n\n第一段已经到达。" if first else "排队补充已送达。"
                tail = "\n\n**完整回答**，保留已显示段落。" if first else ""
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.end_headers()

                def event(payload):
                    self.wfile.write(("data: " + json.dumps(payload) + "\n\n").encode())
                    self.wfile.flush()

                def fragment(text):
                    if protocol == "openai-responses":
                        event({"type": "response.output_text.delta", "item_id": "msg_live", "output_index": 0, "content_index": 0, "delta": text})
                    else:
                        event({"choices": [{"index": 0, "delta": {"content": text}}]})

                fragment(head)
                if hold_all or first:
                    while not release.wait(0.75):
                        self.wfile.write(b": keep-alive\n\n")
                        self.wfile.flush()
                    fragment(tail)
                if protocol == "openai-responses":
                    event({"type": "response.completed", "response": {"id": "response_live", "status": "completed", "output": [
                        {"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": head + tail}]}]}})
                else:
                    event({"choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]})
                self.wfile.write(b"data: [DONE]\n\n")
                self.wfile.flush()
            except Exception as exc:
                errors.append(str(exc))

    httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{httpd.server_port}/v1", release, requests, errors
    finally:
        release.set()
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=5)


def seed(ctx, tmp_path, url, model, *, session_ids=("live",)):
    project = Path(os.environ.get("UI_SMOKE_PROJECT", str(tmp_path / "project")))
    if not project.exists():
        project.mkdir()
        (project / "pyproject.toml").write_text('[project]\nname="live-check"\n', encoding="utf8")
    sessions = [{"id": sid, "title": f"Conversation {sid}", "projectId": "p", "provider": "local",
                 "modelSelection": {"connection_id": "local", "model": model, "base_url": url}, "messages": []}
                for sid in session_ids]
    ctx.save_ui_state({"active_id": session_ids[0], "projects": [{"id": "p", "name": project.name, "path": str(project)}],
        "sessions": sessions}, base_revision=0)


def open_live(pw, httpd):
    browser = pw.chromium.launch(headless=True)
    page = browser.new_page(viewport={"width": 1440, "height": 1000})
    page.set_default_timeout(15000)
    errors = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    page.goto(httpd.launch_url(f"http://127.0.0.1:{httpd.server_port}/"))
    expect(page.locator("#task")).to_be_enabled()
    expect(page.locator("#provider-button")).to_contain_text("fixture")
    return browser, page, errors


def send(page, text):
    page.locator("#task").fill(text)
    expect(page.locator("#send")).to_be_enabled()
    page.locator("#send").click()


def capture(page, tmp_path, name):
    root = Path(os.environ.get("UI_SMOKE_ARTIFACT_DIR", str(tmp_path)))
    root.mkdir(parents=True, exist_ok=True)
    page.screenshot(path=str(root / name), full_page=True)


@pytest.mark.parametrize("protocol", ["openai-completions", "openai-responses"])
def test_real_stream_and_queued_input_keep_one_answer_and_survive_reload(tmp_path, protocol):
    with streaming_peer(protocol) as (url, release, requests, peer_errors):
        target = GateTarget(url, "live-fixture", 32768, 8192, 12000, api_protocol=protocol, request_timeout=30)
        with gate_application(target, tmp_path / "gate") as (httpd, ctx, events), sync_playwright() as pw:
            seed(ctx, tmp_path, url, target.model)
            browser, page, errors = open_live(pw, httpd)
            try:
                send(page, "只读分析项目，先给我一段说明。不要修改文件，不要运行命令。")
                first = page.locator(".msg.asst .body").first
                expect(first).to_contain_text("第一段已经到达。")
                stable = first.locator("p").first.element_handle()
                expect(page.locator(".process-summary .spinner")).to_have_count(1)
                send(page, "只读：下一轮补充说明，不要修改文件。")
                expect(page.locator(".pending-inputs")).to_contain_text("Queued")
                assert len(requests) == 1
                release.set()
                expect(first).to_contain_text("完整回答")
                assert stable.evaluate("el => el.isConnected")
                expect(page.locator(".msg.asst").filter(has_text="排队补充已送达。")).to_have_count(1)
                expect(page.locator(".spinner:visible")).to_have_count(0)
                expect(page.locator(".msg.user")).to_have_count(2)
                expect(page.locator(".msg.asst")).to_have_count(2)
                assert len(requests) == 2 and not peer_errors
                assert any(event["type"] == "reply_stream" and event["state"] == "partial" for event in events)
                assert ctx.input_queue.list()[0]["state"] == "completed"
                page.wait_for_function("document.querySelector('#send').disabled")
                page.reload()
                expect(page.locator(".msg.asst")).to_have_count(2)
                expect(page.locator(".spinner:visible")).to_have_count(0)
                capture(page, tmp_path, f"stream-queue-{protocol}.png")
                assert not errors
            finally:
                release.set()
                browser.close()


@pytest.mark.parametrize("protocol", ["openai-completions", "openai-responses"])
def test_real_two_sessions_run_concurrently_and_stop_is_session_scoped(tmp_path, protocol):
    """Two real HTTP/model runs must be admitted without a global busy error."""
    with streaming_peer(protocol) as (url, release, requests, peer_errors):
        target = GateTarget(url, "live-fixture", 32768, 8192, 12000, api_protocol=protocol, request_timeout=30)
        with gate_application(target, tmp_path / "gate") as (httpd, ctx, events), sync_playwright() as pw:
            seed(ctx, tmp_path, url, target.model, session_ids=("a", "b"))
            browser, page, errors = open_live(pw, httpd)
            try:
                send(page, "会话 A：只读分析，不要修改文件。")
                expect(page.locator(".msg.asst .body").first).to_contain_text("第一段已经到达。")
                page.locator("#btn-new-chat").click()
                expect(page.locator("#task")).to_be_enabled()
                send(page, "会话 B：独立只读分析，不要修改文件。")
                deadline = time.monotonic() + 10
                while len(requests) < 2 and time.monotonic() < deadline:
                    page.wait_for_timeout(100)
                expect(page.locator(".msg.user")).to_have_count(1)
                assert len(requests) == 2, f"second session was not admitted while A was streaming; errors={errors}; peer={peer_errors}; events={[e.get('type') for e in events]}"
                assert not any("Another chat is running" in text for text in errors)
                release.set()
                expect(page.locator(".msg.asst")).to_have_count(1)
                page.locator(".session-item").filter(has_text="Conversation a").click()
                expect(page.locator(".msg.asst")).to_have_count(1)
                assert not peer_errors and not errors
            finally:
                release.set()
                browser.close()


@pytest.mark.parametrize("protocol", ["openai-completions", "openai-responses"])
def test_real_three_sessions_run_concurrently_and_stop_only_targets_selected_session(tmp_path, protocol):
    """Three native runs use independent session lanes and a scoped Stop."""
    with streaming_peer(protocol, hold_all=True, max_requests=3) as (url, release, requests, peer_errors):
        target = GateTarget(url, "live-fixture", 32768, 8192, 12000, api_protocol=protocol, request_timeout=30)
        with gate_application(target, tmp_path / "gate") as (httpd, ctx, events), sync_playwright() as pw:
            seed(ctx, tmp_path, url, target.model, session_ids=("a", "b", "c"))
            browser, page, errors = open_live(pw, httpd)
            stop_requests = []
            page.on("request", lambda request: stop_requests.append(request.post_data_json) if request.url.endswith("/api/stop") else None)
            try:
                send(page, "会话 A：保持流式运行。")
                expect(page.locator(".msg.asst .body").first).to_contain_text("第一段已经到达。")
                page.locator(".session-item").filter(has_text="Conversation b").click()
                send(page, "会话 B：保持流式运行。")
                page.locator(".session-item").filter(has_text="Conversation c").click()
                send(page, "会话 C：保持流式运行。")
                deadline = time.monotonic() + 15
                while len(requests) < 3 and time.monotonic() < deadline:
                    page.wait_for_timeout(100)
                assert len(requests) == 3, f"only {len(requests)} native requests; errors={errors}; events={events}"
                assert not any("Another chat is running" in text for text in errors)

                page.locator(".session-item").filter(has_text="Conversation b").click()
                expect(page.locator("#stop")).to_be_visible()
                b_run_id = ctx.active_run_for(session_id="b").run_id
                with page.expect_response(lambda response: response.url.endswith("/api/stop") and response.request.method == "POST") as stop_response:
                    page.locator("#stop").click()
                assert stop_response.value.ok
                deadline = time.monotonic() + 5
                while not ctx.run_registry.stop_flag_for(b_run_id).is_set() and time.monotonic() < deadline:
                    page.wait_for_timeout(100)
                assert ctx.run_registry.stop_flag_for(b_run_id).is_set(), f"Stop request was not applied; request={stop_requests}"
                deadline = time.monotonic() + 20
                while ctx.active_run_for(session_id="b") is not None and time.monotonic() < deadline:
                    page.wait_for_timeout(100)
                assert ctx.active_run_for(session_id="b") is None, f"Stop did not settle session B; request={stop_requests}; active={ctx.run_registry.active_runs()}"
                assert ctx.active_run_for(session_id="a") is not None or ctx.active_run_for(session_id="c") is not None
                release.set()
                page.locator(".session-item").filter(has_text="Conversation a").click()
                expect(page.locator(".msg.asst .body")).to_contain_text("完整回答")
                page.locator(".session-item").filter(has_text="Conversation c").click()
                expect(page.locator(".msg.asst .body")).to_contain_text("排队补充已送达。")
                assert not errors
            finally:
                release.set()
                browser.close()


@pytest.mark.parametrize("protocol", ["openai-completions", "openai-responses"])
def test_real_question_card_resumes_the_native_run_and_opens_read_details(tmp_path, protocol):
    with question_peer(protocol) as (url, requests, peer_errors):
        target = GateTarget(url, "question-fixture", 32768, 8192, 12000, api_protocol=protocol, request_timeout=20)
        with gate_application(target, tmp_path / "gate") as (httpd, ctx, events), sync_playwright() as pw:
            seed(ctx, tmp_path, url, target.model)
            browser, page, errors = open_live(pw, httpd)
            try:
                send(page, "只读检查 pyproject.toml，先问我范围。不要修改文件，不要运行命令。")
                card = page.locator(".question-card")
                expect(card.locator(".question-title")).to_have_text("检查范围？")
                expect(page.locator(".spinner:visible")).to_have_count(0)
                expect(page.locator("#send")).to_be_disabled()
                card.get_by_role("radio", name="只读文档", exact=True).check()
                card.get_by_role("button", name="Confirm", exact=True).click()
                expect(card).to_contain_text("Answered · 只读文档")
                expect(page.locator(".msg.asst")).to_contain_text("已完成只读文档检查")
                expect(page.locator(".spinner:visible")).to_have_count(0)
                detail = page.locator(".tool-disclosure").filter(has_text="pyproject.toml")
                page.locator(".process-block").filter(has=detail).locator(".process-summary").click()
                detail.locator("summary").click()
                expect(detail.locator("pre")).to_contain_text("[project]")
                assert len(requests) == 3 and not peer_errors and not errors
                capture(page, tmp_path, f"question-{protocol}.png")
            finally:
                browser.close()
