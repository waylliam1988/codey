"""Production Chat commits one linked answer for live and reopened readers."""
from __future__ import annotations

import json
import os
import threading
from collections.abc import Mapping
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

import pytest
from playwright.sync_api import expect, sync_playwright

from codey.app import server
from codey.app.context import AppContext
from codey.app.headless_runner import HeadlessRequest, run_headless
from codey.env_names import NATIVE_TOOLS_ENV
from codey.providers import api_generation
from codey.providers.api_provider import ApiProvider
from codey.types.json_value import JsonObject, decode_json_object
from tools.chat_answer_format_gate import SOURCE, fixed_web

DRAFT = "锅边糊用米浆沿锅边烫熟，适合作早餐[s1]。"
ANSWER = "锅边糊用米浆沿锅边烫熟，适合作早餐[s1](<" + SOURCE + ">)。"
pytestmark = pytest.mark.usefixtures("scripted_local_api_connection", "no_external_advisor_models")


def run_fixture_chat(root: Path, *, native: bool, unknown: bool = False,
                     answer: str = DRAFT, output_format: str = "text") -> tuple[str, list[JsonObject]]:
    requests: list[JsonObject] = []
    events: list[JsonObject] = []

    def generate(endpoint: str, payload: Mapping[str, object], headers: Mapping[str, str], **kwargs: object) -> JsonObject:
        requests.append(decode_json_object(payload))
        index = len(requests)
        if index == 1:
            name, args = "web_search", {"query": "福州早餐"}
        elif index == 2:
            name, args = "open_url", {"url": SOURCE}
        else:
            if unknown and index == 4:
                assert "Unknown source label [s99]" in json.dumps(payload, ensure_ascii=False)
            name, args = "done", {"summary": "未知来源[s99]" if unknown and index == 3 else answer}
        content = json.dumps({"tool": name, "args": args}, ensure_ascii=False)
        message: JsonObject = ({"role": "assistant", "content": "", "tool_calls": [{"id": f"c{index}",
            "type": "function", "function": {"name": name, "arguments": json.dumps(args, ensure_ascii=False)}}]}
            if native else {"role": "assistant", "content": content})
        return {"choices": [{"message": message, "finish_reason": "tool_calls" if native else "stop"}]}

    def connect(provider_id: str, **kwargs: object) -> ApiProvider:
        assert provider_id == "local"
        return ApiProvider("http://scripted.test/v1", "scripted-fixture", native_tools=native,
                           text_tool_format="plain" if native else "json_schema", thinking_enabled=False)

    def observe(row: dict[str, object]) -> None:
        events.append(decode_json_object(row))

    with (patch.dict(os.environ, {NATIVE_TOOLS_ENV: "1" if native else "0"}),
          patch.object(api_generation, "generate", generate), fixed_web()):
        result = run_headless(HeadlessRequest(None, "上网查看福州早餐，并给出来源。", provider_id="local",
            intent="chat", max_turns=5, state_home=root, session_id="citations", run_id="run-citations",
            project_changes_required=False, output_format=output_format,
            model_selection={"model": "scripted-fixture", "base_url": "http://scripted.test/v1"}),
            emit_jsonl=observe, connect_provider=connect)
    assert result.exit_code == 0, events
    terminal = next(row for row in reversed(events) if row.get("type") == "task_done")
    assert terminal["stop_reason"] == "done"
    assert len(requests) == (4 if unknown else 3)
    summary = terminal["summary"]
    assert isinstance(summary, str)
    return summary, requests


@pytest.mark.parametrize("native", [False, True])
@pytest.mark.parametrize("unknown", [False, True])
def test_real_entry_repairs_unknown_ids_and_commits_the_rendered_answer_once(
    tmp_path: Path, native: bool, unknown: bool,
) -> None:
    summary, requests = run_fixture_chat(tmp_path, native=native, unknown=unknown)
    assert summary == ANSWER
    assert "The application generates URLs" in json.dumps(requests[0], ensure_ascii=False)
    with closing(AppContext(tmp_path, start_scheduler=False)) as ctx:
        finals = [entry for entry in ctx.durable.entries("citations") if entry.kind == "assistant_message"]
        assert len(finals) == 1 and finals[0].payload["text"] == summary
        history = decode_json_object(ctx.durable.history_page("citations"))
        items = history["items"]
        assert isinstance(items, list)
        assert any(isinstance(row, dict) and row.get("text") == summary for row in items)
        assert ctx.durable.task("run-citations").status == "completed"


@pytest.mark.parametrize("native", [False, True])
@pytest.mark.parametrize("answer,output_format", [
    ('{"literal":"[s999]","foods":["锅边糊"]}', "json"),
    ('```python\nprint("[s999]")\n```', "text"),
])
def test_literal_formats_survive_the_same_entry_and_persistence_path(
    tmp_path: Path, native: bool, answer: str, output_format: str,
) -> None:
    summary, _ = run_fixture_chat(tmp_path, native=native, answer=answer, output_format=output_format)
    assert summary == answer
    with closing(AppContext(tmp_path, start_scheduler=False)) as ctx:
        finals = [entry for entry in ctx.durable.entries("citations") if entry.kind == "assistant_message"]
        assert len(finals) == 1 and finals[0].payload["text"] == answer


def test_reopened_application_display_and_clipboards_use_the_committed_link(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    summary, _ = run_fixture_chat(tmp_path, native=False)
    ctx = AppContext(tmp_path, start_scheduler=False)
    ctx.save_ui_state({"active_id": "citations", "projects": [], "sessions": [
        {"id": "citations", "title": "程序化引用"}]}, base_revision=0)
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
            link = page.locator(".asst .body a", has_text="s1")
            expect(link).to_have_attribute("href", SOURCE)
            page.locator(".asst").hover()
            page.locator(".asst .msg-copy").click()
            assert page.evaluate("navigator.clipboard.readText()") == summary
            page.locator(".asst .body").click(position={"x": 5, "y": 5})
            page.keyboard.press("Control+a")
            page.keyboard.press("Control+c")
            copied = page.evaluate("""async () => {
                for (const item of await navigator.clipboard.read()) {
                    if (item.types.includes('text/html')) return (await item.getType('text/html')).text();
                }
                return '';
            }""")
            assert isinstance(copied, str) and SOURCE in copied
            page.reload()
            expect(page.locator(".asst .body a", has_text="s1")).to_have_attribute("href", SOURCE)
            browser.close()
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(5)
        ctx.close()
