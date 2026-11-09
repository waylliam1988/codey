"""Controlled acquisition diagnostics through the real local model and task kernel.

Network responses are fixtures; provider requests, tool execution, receipts and
completion use production. These checks certify contracts, not answer quality.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
from pathlib import Path
from unittest.mock import patch
from urllib.request import Request

from playwright.sync_api import Route

from codey.app.headless_runner import HeadlessRequest, run_headless
from codey.env_names import NATIVE_TOOLS_ENV
from codey.types.json_value import JsonObject, decode_json_object
from tools import local_model_gate_attempts as attempts
from tools.shell_web_diagnostic_gate import select_target
from tools.shell_web_food_gate import opened_bodies
from tools.spa_acquisition_comparison_gate import FixtureResponse

URL = "https://example.com/versioned-field-record"
FAILURE_URLS = ["https://example.com/restricted", "https://example.com/blank",
                "https://example.com/script"]
ORIGINAL = ("Original field record.\n\n" + "Background paragraph about the field study.\n\n" * 165
            + "Saffron quantity is exactly 17 grams in the original record.\n\n" * 12)
UPDATED = "Updated record. The revised ingredient quantity is 99 grams.\n\n" * 90


def run(directory: Path, batch: int, timeout: float) -> JsonObject:
    target = select_target("gemma", timeout, "http://127.0.0.1:5001/v1")
    task = (
        f"Run this source-reading diagnostic using real tools, in this exact order, one step per turn: "
        f"1. open_url {URL}. 2. source_search that URL for saffron. "
        f"3. open_url the same URL at offset 0 to explicitly acquire its changed online version. "
        "4. open_hit using the hit_id from step 2; it must refer to the original captured version. "
        "5. reopen_source using the observed source_id. "
        f"6. open_url {URL} at offset 1000 to continue the captured body. "
        "Finally report the quantities seen in step 3 and step 4. Do not search the web, skip steps, "
        "or read stored knowledge. This is a diagnostic of captured source versions."
    )
    research = batch == 2
    if research:
        task = (f"Research the measured saffron quantity in this field record: {URL}. "
                "Read the URL, then use source_search for saffron and open_url with the returned offset "
                "to inspect the original finding. Use only this URL. Submit a short research answer "
                "with its original eN citation. Do not save knowledge updates. This diagnostic requires "
                "both the initial read and a subsequent locator read, even if the first read fits in context.")
    if batch == 3:
        task = ("Run this page acquisition diagnostic. Open each URL once in this order: "
                + ", ".join([*FAILURE_URLS, URL])
                + ". The first three may fail; continue to the next URL after each failure and do not retry. "
                "Finally report the observed limitations and the saffron quantity from the readable page. "
                "Do not search, save knowledge, or claim to have read failed pages.")
    acquisitions: list[JsonObject] = []

    def http(request: Request, *, timeout: float) -> FixtureResponse:
        body = "Field record: saffron quantity is exactly 17 grams." if batch == 3 else ORIGINAL if not acquisitions else UPDATED
        acquisitions.append({"url": request.full_url, "version": len(acquisitions) + 1})
        (directory / "acquisitions.json").write_text(json.dumps(acquisitions), encoding="utf-8")
        html = "<html><title>Field record</title><body><article>" + body + "</article></body></html>"
        if request.full_url == FAILURE_URLS[0]:
            html = "<html><body>Access restricted</body></html>"
        elif request.full_url == FAILURE_URLS[1]:
            html = "<html><body></body></html>"
        elif request.full_url == FAILURE_URLS[2]:
            html = "<html><body><div id='root'></div><script src='/bootstrap.js'></script></body></html>"
        response = FixtureResponse(request.full_url, html)
        if request.full_url == FAILURE_URLS[0]:
            response.status = 403
        return response

    def browser_response(self: object, route: Route) -> None:
        if route.request.resource_type == "script":
            route.fulfill(status=200, content_type="text/javascript", body="throw new Error('Observed script failure');")
            return
        acquisitions.append({"url": route.request.url, "backend": "browser", "version": 1})
        (directory / "acquisitions.json").write_text(json.dumps(acquisitions), encoding="utf-8")
        html = ("<html><body><div id='root'></div><script src='/bootstrap.js'></script></body></html>"
                if batch == 3 else "<html><title>Field record</title><body><article>" + ORIGINAL + "</article></body></html>")
        route.fulfill(status=200, content_type="text/html; charset=utf-8", body=html)

    def observe(row: dict[str, object]) -> None:
        with (directory / "events.jsonl").open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(row, ensure_ascii=False) + "\n")

    def connect(provider_id: str, **kwargs: object) -> attempts.RecordingProvider:
        if provider_id != "local":
            raise ValueError("diagnostic requires the selected local model")
        return attempts.RecordingProvider(target, directory)

    manifest = {"batch": batch, "scope": "controlled_sources_real_model_production_headless_kernel",
                "model": target.model, "temperature": target.temperature, "task": task,
                "git_head": subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip()}
    (directory / "manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    with (
        patch.dict(os.environ, {NATIVE_TOOLS_ENV: "1"}),
        attempts.isolate_gate_secondary_models(target),
        attempts.record_api_generations(directory / "provider.jsonl"),
        patch("codey.research.source_download._open_url_no_redirect", http),
        patch("codey.research.browser_search.BrowserSearchProvider._guard_request", browser_response),
    ):
        result = run_headless(HeadlessRequest(None, task, intent="research" if research else "chat", provider_id="local", max_turns=10,
            state_home=directory / "state", model_selection={"model": target.model, "base_url": target.base_url},
            research_store_root=directory / "knowledge" if research else None,
            requested_capabilities=(), project_changes_required=False), emit_jsonl=observe, connect_provider=connect)
    return analyze(directory, manifest, acquisitions, result.stop_reason, research=research)


def analyze(directory: Path, manifest: JsonObject, acquisitions: list[JsonObject], stop_reason: str, *,
            research: bool) -> JsonObject:
    events = [decode_json_object(json.loads(line)) for line in
              (directory / "events.jsonl").read_text(encoding="utf-8").splitlines()]
    tool_rows = [row for row in events if row.get("type") == "tool"]
    bodies = opened_bodies(directory)
    required = {"source_search", "open_hit", "reopen_source", "open_url"}
    passed = (stop_reason == "done" and len(acquisitions) == 2
              and required <= {str(row.get("tool_name")) for row in tool_rows}
              and all(row.get("ok") is True for row in tool_rows)
              and len(bodies) >= 3 and "99 grams" in bodies[1] and "17 grams" in bodies[2])
    if research:
        requests = [decode_json_object(json.loads(line)) for line in
                    (directory / "provider.jsonl").read_text(encoding="utf-8").splitlines()]
        delivered = [json.dumps(row["payload"], ensure_ascii=False) for row in requests if row.get("type") == "request"]
        passed = (stop_reason == "done" and len(acquisitions) == 1
                  and sum(row.get("tool_name") in {"open_url", "open_hit", "reopen_source"} for row in tool_rows) >= 2
                  and any(row.get("tool_name") == "source_search" for row in tool_rows)
                  and all(row.get("ok") is True for row in tool_rows)
                  and any("17 grams" in body and "[e1]" in body for body in delivered))
    if manifest["batch"] == 3:
        failures = [row for row in tool_rows if row.get("ok") is False]
        codes = {decode_json_object(row["failure"])["code"] for row in failures if "failure" in row}
        passed = (stop_reason in {"done", "blocked"} and len(failures) == 3 and len(tool_rows) == 4
                  and codes == {"source.access_restricted", "source.page_not_loaded", "source.render_script_error"}
                  and len(acquisitions) == 5 and any(row.get("ok") is True for row in tool_rows))
    report: JsonObject = {**manifest, "passed": passed, "stop_reason": stop_reason,
        "acquisitions": acquisitions, "tools": tool_rows, "delivered_bodies": bodies,
        "limits": "Diagnostic tool instructions and controlled HTTP responses; no answer quality claim."}
    (directory / "summary.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--batch", type=int, choices=(1, 2, 3, 4), required=True)
    parser.add_argument("--run-dir", type=Path, required=True)
    parser.add_argument("--timeout", type=float, default=180)
    args = parser.parse_args()
    directory = args.run_dir.resolve()
    directory.mkdir(parents=True, exist_ok=False)
    report = run(directory, args.batch, args.timeout)
    print(json.dumps({"passed": report["passed"], "stop_reason": report["stop_reason"]}), flush=True)
    return 0 if report["passed"] is True else 1


if __name__ == "__main__":
    raise SystemExit(main())
