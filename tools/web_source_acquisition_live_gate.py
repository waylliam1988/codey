"""Exercise shared acquisition through the production local-model task entry.

Only network responses and the browser's headless configuration are fixtures.
Provider selection, HTTP/browser choice, kernel, receipts and completion are real.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
from collections.abc import Callable
from pathlib import Path
from unittest.mock import patch
from urllib.request import Request

from playwright.sync_api import Route

from codey.app.headless_runner import HeadlessRequest, run_headless
from codey.durable import DurableSession, SQLiteDurableStore
from codey.env_names import NATIVE_TOOLS_ENV
from codey.operations.kernel_receipts import restore_result_receipt
from codey.research.browser_search import BrowserSearchProvider
from codey.research.web_sources import BrowserSource, WebSourceProvider
from codey.runtime.effects.effect_records import EFFECT_CATEGORY_TOOL_CALL, RuntimeEffectStore
from codey.storage.managed_outputs import ManagedOutputStore
from codey.types.json_value import JsonObject, JsonValue, decode_json_object
from tools import local_model_gate_attempts as attempts
from tools.browser_research_cases import html, pdf
from tools.shell_web_diagnostic_gate import select_target
from tools.source_convergence_live_gate import FixtureResponse

ROOT = "https://example.com/acquisition-convergence/"
STATIC, RENDER, PDF = ROOT + "static", ROOT + "rendered", ROOT + "record.pdf"
RENDER_BODY = "Original saffron field record: the measured net quantity is exactly 17 grams, excluding packaging."


def saved_acquisitions(directory: Path, events: list[JsonObject]) -> list[JsonObject]:
    start = next(row for row in events if row.get("type") == "task_start")
    session_id, run_id = str(start["session_id"]), str(start["run_id"])
    home = directory / "state"
    durable = DurableSession(SQLiteDurableStore(home / "durable.sqlite3"))
    records: list[JsonObject] = []
    try:
        for projection in RuntimeEffectStore(durable=durable).load_effects(session_id, run_id):
            if projection.intent.effect_category != EFFECT_CATEGORY_TOOL_CALL or not projection.is_settled:
                continue
            result = restore_result_receipt(projection, store=ManagedOutputStore(home), session_id=session_id, run_id=run_id)
            if "source_acquisition" in result.canonical:
                records.append({"args": result.call.args, "canonical": result.canonical, "model_text": result.model_text})
    finally:
        durable.close()
    return records


def analyze(records: list[JsonObject], acquisitions: list[JsonObject], events: list[JsonObject],
            stop_reason: str, *, require_locators: bool = True) -> JsonObject:
    traces: JsonObject = {}
    texts: list[str] = []
    for row in records:
        args = decode_json_object(row["args"])
        canonical = decode_json_object(row["canonical"])
        trace = decode_json_object(canonical["source_acquisition"])
        entries = trace["attempts"]
        assert isinstance(entries, list)
        traces[str(args["url"])] = [decode_json_object(entry)["backend"] for entry in entries]
        texts.append(str(row.get("model_text") or ""))
    tools = [row for row in events if row.get("type") == "tool"]
    names = {row.get("tool_name") for row in tools}
    expected_calls: list[JsonObject] = [
        {"backend": "http", "url": STATIC}, {"backend": "http", "url": RENDER},
        {"backend": "browser", "url": RENDER}, {"backend": "http", "url": PDF},
    ]
    expected_traces: JsonObject = {STATIC: ["http"], RENDER: ["http", "browser"], PDF: ["http"]}
    locator_check = not require_locators or {"source_search", "open_hit", "reopen_source"} <= names
    passed = (stop_reason == "done" and acquisitions == expected_calls and traces == expected_traces
              and len(records) == 3 and locator_check
              and all(row.get("ok") is True for row in tools)
              and all(any(value in text for text in texts) for value in ("61 grams", "17 grams", "37 grams")))
    return {"passed": passed, "stop_reason": stop_reason, "acquisitions": acquisitions,
            "traces": traces, "tools": tools, "original_reads": texts,
            "limits": "Controlled source responses and diagnostic task; no general answer-quality claim."}


def run(directory: Path, intent: str, timeout: float) -> JsonObject:
    target = select_target("gemma", timeout, "http://127.0.0.1:5001/v1")
    task = ("Run this source acquisition diagnostic with real source tools, in exactly this order, one step per turn: "
            f"1. open_url {STATIC}. 2. open_url {RENDER}. 3. open_url {PDF}. "
            f"4. source_search {RENDER} for saffron. 5. open_hit the observed hit from step 4. "
            "6. reopen_source the source_id of the saffron record. "
            "Then report the three original measured quantities with their eN citations. "
            "Use only these sources. Do not search the web, repeat acquisitions, or save knowledge. "
            "This diagnostic requires all six tool steps even if the quantities are already visible.")
    if intent == "research":
        task = (f"Research the net measured quantities in these three original records: {STATIC}, {RENDER}, {PDF}. "
                "Read each URL once in the listed order, then submit_research_answer with the three quantities "
                "and their original eN citations, including whether packaging is excluded. "
                "Use only these three sources; do not search, reacquire a page, or save knowledge.")
    documents = {STATIC: html("<article>Static field record: net quantity is 61 grams, excluding packaging.</article>"),
                 PDF: pdf("PDF field record: net quantity is 37 grams, excluding packaging.")}
    acquisitions: list[JsonObject] = []
    events: list[JsonObject] = []

    def record(backend: str, url: str) -> None:
        acquisitions.append({"backend": backend, "url": url})
        (directory / "acquisitions.json").write_text(json.dumps(acquisitions), encoding="utf-8")

    def http(request: Request, *, timeout: float) -> FixtureResponse:
        record("http", request.full_url)
        if request.full_url == RENDER:
            response = FixtureResponse(RENDER, "<body>HTTP access denied; no original quantity is available.</body>")
            response.status = 403
            return response
        document = documents[request.full_url]
        response = FixtureResponse(request.full_url, "")
        response.headers = {"content-type": document.content_type}
        response.write(document.body)
        response.seek(0)
        return response

    def browser_response(self: BrowserSearchProvider, route: Route) -> None:
        record("browser", route.request.url)
        if route.request.url != RENDER:
            raise ValueError("static/PDF unexpectedly entered browser acquisition")
        body = ("<html><body><app-root></app-root><script>setTimeout(() => {"
                "document.querySelector('app-root').innerText=" + json.dumps(RENDER_BODY)
                + ";}, 5500);</script></body></html>")
        route.fulfill(status=200, content_type="text/html; charset=utf-8", body=body)

    original_init = WebSourceProvider.__init__

    def headless(self: WebSourceProvider, *, browser_factory: Callable[[], BrowserSource] | None = None) -> None:
        original_init(self, browser_factory=browser_factory or (lambda: BrowserSearchProvider(headless=True)))

    def observe(row: dict[str, object]) -> None:
        event = decode_json_object(row)
        events.append(event)
        with (directory / "events.jsonl").open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(event, ensure_ascii=False) + "\n")

    def connect(provider_id: str, **kwargs: object) -> attempts.RecordingProvider:
        if provider_id != "local":
            raise ValueError("diagnostic requires the selected local model")
        return attempts.RecordingProvider(target, directory)

    manifest: JsonObject = {"intent": intent, "model": target.model, "temperature": target.temperature,
        "task": task, "scope": "production_headless_provider_kernel_default_entry_with_headless_browser_fixture",
        "git_head": subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip()}
    (directory / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    with (patch.dict(os.environ, {NATIVE_TOOLS_ENV: "1"}),
          attempts.isolate_gate_secondary_models(target),
          attempts.record_api_generations(directory / "provider.jsonl"),
          patch.object(WebSourceProvider, "__init__", headless),
          patch("codey.research.source_download._open_url_no_redirect", http),
          patch.object(BrowserSearchProvider, "_guard_request", browser_response)):
        result = run_headless(HeadlessRequest(None, task, intent=intent, provider_id="local", max_turns=10,
            state_home=directory / "state", model_selection={"model": target.model, "base_url": target.base_url},
            research_store_root=directory / "knowledge" if intent == "research" else None,
            project_changes_required=False), emit_jsonl=observe, connect_provider=connect)
    report = {**manifest, **analyze(saved_acquisitions(directory, events), acquisitions, events, result.stop_reason,
                                  require_locators=intent == "chat")}
    (directory / "summary.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-dir", type=Path, required=True)
    parser.add_argument("--intent", choices=("chat", "research"), required=True)
    parser.add_argument("--timeout", type=float, default=180)
    args = parser.parse_args()
    directory = args.run_dir.resolve()
    directory.mkdir(parents=True, exist_ok=False)
    report = run(directory, args.intent, args.timeout)
    result: dict[str, JsonValue] = {"passed": report["passed"], "stop_reason": report["stop_reason"]}
    print(json.dumps(result), flush=True)
    return 0 if report["passed"] is True else 1


if __name__ == "__main__":
    raise SystemExit(main())
