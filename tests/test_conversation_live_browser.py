"""Production UI -> HTTP -> native provider -> SSE, without injecting UI events."""
from __future__ import annotations

import json
import os
import threading
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest
from playwright.sync_api import expect, sync_playwright

from tests.test_conversation_question_http import question_peer
from tools.local_model_gate_attempts import GateTarget
from tools.local_model_gate_shell import gate_application


@contextmanager
def streaming_peer(protocol):
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
                assert len(requests) <= 2, "unexpected repeated request"
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
                if first:
                    assert release.wait(25), "browser did not release the stream"
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


def seed(ctx, tmp_path, url, model):
    project = Path(os.environ.get("UI_SMOKE_PROJECT", str(tmp_path / "project")))
    if not project.exists():
        project.mkdir()
        (project / "pyproject.toml").write_text('[project]\nname="live-check"\n', encoding="utf8")
    ctx.save_ui_state({"active_id": "live", "projects": [{"id": "p", "name": project.name, "path": str(project)}],
        "sessions": [{"id": "live", "title": "Conversation live check", "projectId": "p", "provider": "local",
            "modelSelection": {"connection_id": "local", "model": model, "base_url": url}, "messages": []}]}, base_revision=0)


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
