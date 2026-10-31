"""Opt-in real Local inference and browser reload check for thinking/tool progress."""
from __future__ import annotations

import argparse
import json
import os
import sys
import threading
import time
import urllib.request
from dataclasses import asdict
from pathlib import Path

# A baseline checkout can run this same harness using its own production code.
sys.path.insert(0, str(Path.cwd()))

from codey.app import server as codey_server
from codey.app.context import AppContext
from codey.app.headless_runner import HeadlessRequest, run_headless
from codey.app.public_event_feed import PublicEventFeed
from codey.providers import local_config
from codey.providers.local_config import LocalContextBudget, LocalProviderConfig
from tools.local_model_gate_attempts import GateTarget, isolate_gate_secondary_models, record_api_generations
from tools.local_model_ui_gate import _configure_gate_model_preferences

MARKER = "AUDIT_MARKER_731_20261031"
QUESTIONS = ["网站有哪些 API 端点？", "认证机制是什么，是否有绕过可能？",
             "是否存在 SQL 注入、XSS、CSRF？", "API 是否有速率限制或枚举风险？", "是否存在信息泄露？"]
SOURCE = '''import sqlite3
from http.server import BaseHTTPRequestHandler
from urllib.parse import parse_qs, urlsplit

AUDIT_MARKER = "AUDIT_MARKER_731_20261031"

class DemoHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        route = urlsplit(self.path)
        args = parse_qs(route.query)
        if route.path == "/api/users":
            name = args.get("name", [""])[0]
            rows = sqlite3.connect("demo.db").execute("SELECT id,name FROM users WHERE name='" + name + "'").fetchall()
            self.send_response(200)
            self.end_headers()
            self.wfile.write(str(rows).encode())
        elif route.path == "/hello":
            name = args.get("name", [""])[0]
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.end_headers()
            self.wfile.write(("<h1>Hello " + name + "</h1>").encode())
        elif route.path == "/debug":
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"demo build 1.2; database=demo.db")
'''
TASK = ("帮我检查这个本地演示项目中的 Web 漏洞。只做源码审计，不执行命令、不修改文件、不访问外部网站。"
        "先用 research_plan 登记这五个问题，然后读取 app.py 继续调查，最后按 q1..q5 逐项回答并注明代码位置。"
        "区分已证实问题与仅凭源码无法判断的风险，不要把计划当作调查完成。问题："
        + json.dumps(QUESTIONS, ensure_ascii=False)
        + "。最后附上源码中 AUDIT_MARKER 的完整值，以证明实际读取了文件。")


def browser_check(state_home: Path, project: Path, session: str, model: str, output: Path) -> dict:
    from playwright.sync_api import expect, sync_playwright

    ctx = AppContext(state_home, start_scheduler=False)
    httpd = None
    feed = PublicEventFeed()
    detach = ctx.event_bus.observe(feed.publish)
    try:
        _configure_gate_model_preferences(ctx, "local", model)
        snapshot = ctx.ui_state_store.load()
        ctx.ui_state_store.save({**snapshot, "active_id": session,
            "projects": [{"id": "fixture", "name": "Local audit fixture", "path": str(project), "expanded": True}],
            "sessions": [{"id": session, "title": "Local thinking live check", "provider": "local", "projectId": "fixture"}]},
            base_revision=snapshot["revision"])
        history = ctx.durable.history_page(session, limit=200)["items"]
        thoughts = [row for row in history if row.get("type") == "turn" and row.get("reasoning")]
        httpd = codey_server.CodeyHTTPServer(("127.0.0.1", 0), codey_server.Handler)
        httpd.app_context, httpd.public_feed = ctx, feed
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        base = f"http://127.0.0.1:{httpd.server_address[1]}/"
        errors = []
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(channel="msedge", headless=True)
            page = browser.new_page(viewport={"width": 1380, "height": 1000})
            page.on("pageerror", lambda error: errors.append(str(error)))
            try:
                page.goto(httpd.launch_url(base), wait_until="domcontentloaded")
                expect(page.locator(".thinking-body")).not_to_be_empty(timeout=20000)
                expect(page.locator(".tl-path", has_text="research plan").first).to_be_attached(timeout=10000)
                before = page.locator(".thinking-body").inner_text()
                page.reload(wait_until="domcontentloaded")
                expect(page.locator(".thinking-body")).not_to_be_empty(timeout=20000)
                after = page.locator(".thinking-body").inner_text()
                checks = {"persisted_thinking": bool(thoughts), "thinking_survives_reload": before == after,
                          "plan_label": page.locator(".tl-path", has_text="research plan").count() > 0,
                          "no_page_errors": not errors}
                page.locator(".process-group").evaluate_all("nodes => nodes.forEach(node => node.open = true)")
                page.locator(".thinking").evaluate_all("nodes => nodes.forEach(node => node.open = true)")
                expect(page.locator(".thinking-body")).to_be_visible(timeout=10000)
                checks["all_thinking_turns_visible"] = page.locator(".thinking-section").count() == len(thoughts)
                page.locator(".thinking > summary").scroll_into_view_if_needed()
                page.screenshot(path=str(output / "thinking-reload.png"), full_page=True)
                return {"checks": checks, "persisted_turns": len(thoughts), "page_errors": errors}
            except Exception:
                page.screenshot(path=str(output / "ui-failure.png"), full_page=True)
                raise
            finally:
                browser.close()
    finally:
        if httpd is not None:
            httpd.shutdown()
            httpd.server_close()
        detach()
        feed.close()
        ctx.close()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--base-url", default="http://127.0.0.1:5001/v1")
    parser.add_argument("--no-browser", action="store_true")
    args = parser.parse_args()
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=False)
    with urllib.request.urlopen(args.base_url.rstrip("/") + "/models", timeout=5) as response:
        model = json.load(response)["data"][0]["id"]
    state_home, project = output / "state", output / "project"
    state_home.mkdir()
    project.mkdir()
    (project / "app.py").write_text(SOURCE, encoding="utf-8")
    local_config.DEFAULT_STATE_HOME = state_home
    local_config.save_local_config(LocalProviderConfig(base_url=args.base_url, model=model,
        native_tools_mode="on", thinking_enabled=True, connection_revision="live-thinking-fixture",
        context=LocalContextBudget(262144, 32768, 32000)))
    os.environ["LOCAL_OPENAI_BASE_URL"], os.environ["LOCAL_OPENAI_MODEL"] = args.base_url, model
    rows = []

    def emit(row):
        rows.append(row)
        with (output / "events.jsonl").open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(row, ensure_ascii=False) + "\n")
        if row.get("type") in {"turn", "tool", "task_done"}:
            print(json.dumps({key: row[key] for key in ("type", "turn", "tool_name", "ok", "stop_reason") if key in row}), flush=True)

    target = GateTarget(args.base_url, model, 262144, 32768, 32000)
    started = time.monotonic()
    report = {"model": model, "task": TASK, "source": str(Path.cwd()), "checks": {}}
    try:
        with record_api_generations(output / "provider.jsonl"), isolate_gate_secondary_models(target):
            result = run_headless(HeadlessRequest(project=project, task=TASK, provider_id="local", intent="chat",
                session_id="thinking_live", max_turns=10, state_home=state_home,
                research_store_root=state_home / "vault", project_changes_required=False,
                model_selection={"base_url": args.base_url, "model": model, "thinking": True}), emit_jsonl=emit)
        terminal = next((row for row in reversed(rows) if row.get("type") == "task_done"), {})
        tools = [row for row in rows if row.get("type") == "tool"]
        summary = str(terminal.get("summary", ""))
        report.update(result=asdict(result), tools=[row.get("tool_name") for row in tools], summary=summary)
        report["checks"] = {"done": result.stop_reason == "done", "read_source": any(
            row.get("tool_name") == "read_file" and row.get("ok") for row in tools),
            "plan_progress": 1 <= sum(row.get("tool_name") == "research_plan" for row in tools) <= 2,
            "source_marker": MARKER in summary, "five_questions": all(f"q{index}" in summary.lower() for index in range(1, 6)),
            "thinking_returned": any(row.get("reasoning") for row in rows),
            "fixture_unchanged": (project / "app.py").read_text(encoding="utf-8") == SOURCE}
        if not args.no_browser:
            report["browser"] = browser_check(state_home, project, result.session_id, model, output)
            report["checks"].update(report["browser"]["checks"])
        report["ok"] = all(report["checks"].values())
    except Exception as exc:
        report.update(ok=False, error=f"{type(exc).__name__}: {exc}")
    finally:
        report["seconds"] = round(time.monotonic() - started, 2)
        (output / "result.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"ok": report.get("ok"), "checks": report["checks"], "error": report.get("error"),
                      "seconds": report["seconds"], "output": str(output)}, ensure_ascii=False), flush=True)
    return 0 if report.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
