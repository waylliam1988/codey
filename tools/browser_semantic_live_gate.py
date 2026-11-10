"""Semantic browser diagnostics through the existing headless/provider/kernel.

Only this process registers candidate tools. Production defaults stay unchanged.
The browser executes real DOM actions; only the test document is intercepted.
"""
from __future__ import annotations

import argparse
import io
import json
import os
import re
import subprocess
import time
from contextlib import ExitStack
from datetime import UTC, datetime
from email.message import Message
from pathlib import Path
from unittest.mock import patch
from urllib.request import Request
from urllib.response import addinfourl

from playwright.sync_api import Route

from codey.app.headless_runner import HeadlessRequest, run_headless
from codey.automation.browser import CdpEndpoint
from codey.env_names import NATIVE_TOOLS_ENV
from codey.operations.task_execution import ExecutionDelegate
from codey.providers.request_context import clock_context
from codey.research import answer_contract
from codey.research.browser_search import BrowserSearchProvider
from codey.research.ledger import ResearchLedger
from codey.research.source_reading import render_source_read
from codey.research.tools import ResearchTools
from codey.runtime.core.models import ToolCall, ToolResult
from codey.toolchain.tool_spec import register_custom_executor, spec_for_tool, unregister_custom_tool
from codey.types.json_value import JsonObject, canonical_json_object, decode_json_object, require_json_integer
from tests.support.real_cdp import cdp_browser
from tools import local_model_gate_attempts as attempts
from tools.browser_experiment_context import browser_action_context
from tools.browser_experiment_interaction import BrowserInteraction
from tools.browser_experiment_sources import BrowserCaptureGateway, ResumableBrowserSearchProvider
from tools.browser_observation_candidates import PROJECTIONS, observation_candidate
from tools.browser_policy_cases import policy_cases, search_hits
from tools.browser_policy_options import GUIDANCE
from tools.browser_research_cases import BrowserResearchCase, Document, cases
from tools.shell_web_diagnostic_gate import select_target

BROWSER_TOOLS = ("browser_snapshot", "browser_click", "browser_fill", "browser_scroll", "browser_wait", "browser_read")
DIAGNOSTIC_SEQUENCE = ("open_url", "browser_snapshot", "browser_fill", "browser_snapshot",
                       "browser_click", "browser_wait", "browser_scroll", "browser_snapshot",
                       "browser_read", "submit_research_answer")
URL = "https://example.com/interactive-field-record"
HTML = """<!doctype html><html><head><meta charset="utf-8"><title>Interactive record</title></head>
<body><article>Interactive field record. Enter the requested locality, then press Load.
The initial page does not contain the measured quantity.</article>
<label>Locality <input aria-label="Locality" id="query"></label>
<button id="load" onclick="window.clicks++; document.querySelector('#result').textContent =
document.querySelector('#query').value === '福州' ? '福州样本：鱼丸净重为 173 克。' : 'Unknown locality';">Load</button>
<p id="result">Awaiting locality</p><div style="height:2000px"></div>
<script>window.clicks=0;</script></body></html>"""


class _FixedRequestClock:
    @staticmethod
    def now() -> datetime:
        return datetime(2026, 11, 9, 10, 0, tzinfo=UTC)


def fixed_request_clock() -> str:
    """Keep the production clock renderer, fixing only its experimental input."""
    with patch("codey.providers.request_context.datetime", _FixedRequestClock):
        return clock_context()


def diagnostic_steps_complete(tools: list[JsonObject]) -> bool:
    expected = iter(DIAGNOSTIC_SEQUENCE)
    current = next(expected)
    for row in tools:
        if row.get("tool_name") == current and row.get("ok") is True:
            current = next(expected, "")
            if not current:
                return True
    return False


def execute_browser(call: ToolCall, browser: BrowserInteraction) -> ToolResult:
    """Candidate tools use the same ordinary ToolDispatch as built-in tools."""
    args = call.args
    try:
        if call.name == "browser_read":
            capture = browser.capture()
            url = str(capture.get("url") or "")
            ledger = ResearchLedger()
            outcome = BrowserCaptureGateway(browser.provider, ledger).accept_capture(url, capture)
            opened = render_source_read(outcome, ledger,
                offset=require_json_integer(args.get("offset", 0), "offset"),
                limit=require_json_integer(args.get("limit", 6000), "limit"), coherent=True)
            delegate = ExecutionDelegate(research_tools=ResearchTools(search=browser.provider,
                                                                     store=None, changes=None, ledger=ledger))
            result, _, _ = delegate._opened_result(call, opened, url, ledger=ledger, turn=0, tool_index=0)
            return result
        if call.name == "browser_snapshot":
            output = browser.snapshot(max_elements=require_json_integer(args.get("max_elements", 80), "max_elements"),
                                      max_text=require_json_integer(args.get("max_text", 12000), "max_text"))
        elif call.name == "browser_click":
            output = browser.click(str(args.get("ref") or ""))
        elif call.name == "browser_fill":
            output = browser.fill(str(args.get("ref") or ""), str(args.get("value") or ""))
        elif call.name == "browser_scroll":
            output = browser.scroll(str(args.get("direction") or ""),
                                    require_json_integer(args.get("amount"), "amount"))
        else:
            output = browser.wait(str(args.get("condition") or ""), str(args.get("value") or ""),
                                  require_json_integer(args.get("timeout_ms"), "timeout_ms"))
    except Exception as exc:
        return ToolResult(call=call, ok=False, model_text=f"ERROR: {exc}")
    return ToolResult(call=call, ok=output.ok, model_text=output.model_text,
                      canonical={"browser_observation": output.observation})


def run(directory: Path, timeout: float, *, candidate: bool, diagnostic: bool, cdp_port: int = 0,
        case: BrowserResearchCase | None = None, guidance: str = "", projection: str = "",
        fixed_clock: bool = False) -> JsonObject:
    started = time.monotonic()
    target = select_target("gemma", timeout, "http://127.0.0.1:5001/v1")
    provider = (ResumableBrowserSearchProvider if candidate else BrowserSearchProvider)(
        headless=not bool(cdp_port), isolated=False, cdp_port=cdp_port or 9262)
    browser = BrowserInteraction(provider) if isinstance(provider, ResumableBrowserSearchProvider) else None
    task = (f"查看 {URL} 中福州样本的鱼丸净重，说明页面实际显示的数值。"
            "只使用这个来源；如果材料不足，请如实说明。不要保存知识。")
    documents = case.documents if case is not None else {URL: Document(HTML.encode("utf-8"))}
    expected = case.expected if case is not None else ("173",)
    if case is not None:
        task = case.task + " 不要保存知识。只使用给定资料；内容不可得时明确说明。"
    if diagnostic:
        task = (f"Browser contract diagnostic. First open_url {URL}. Then browser_snapshot, "
                "browser_fill the Locality textbox with 福州, take a NEW browser_snapshot, "
                "browser_click Load using its NEW ref, browser_wait for visible text 鱼丸, "
                "then browser_scroll down 500 pixels and browser_snapshot. Finally browser_read "
                "to capture the expanded record without navigating. One action per turn. "
                "Finish with submit_research_answer state=answered and cite the ORIGINAL eN passage "
                "from that captured record beside its value. DOM observations alone are not source passages. "
                "Do not invent evidence labels, search the web, reacquire the URL, or save knowledge.")
    events: list[JsonObject] = []
    navigations: list[str] = []

    def observe(row: dict[str, object]) -> None:
        events.append(canonical_json_object(row))
        with (directory / "events.jsonl").open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(row, ensure_ascii=False) + "\n")

    def route_document(_self: object, route: Route) -> None:
        navigations.append(route.request.url)
        document = documents.get(route.request.url)
        if document is None:
            route.fulfill(status=404, content_type="text/html", body="<html><body>Fixture URL was not provided.</body></html>")
        else:
            route.fulfill(status=document.status, content_type=document.content_type, body=document.body)

    def http_document(request: Request, *, timeout: float) -> addinfourl:
        document = documents.get(request.full_url)
        if document is None:
            raise ValueError("fixture URL was not provided: " + request.full_url)
        navigations.append(request.full_url)
        headers = Message()
        headers["Content-Type"] = document.content_type
        headers["Content-Length"] = str(len(document.body))
        return addinfourl(io.BytesIO(document.body), headers, request.full_url, document.status)

    def connect(provider_id: str, **kwargs: object) -> attempts.RecordingProvider:
        if provider_id != "local":
            raise ValueError("diagnostic requires the selected local model")
        return attempts.RecordingProvider(target, directory)

    def source_factory() -> BrowserSearchProvider:
        return provider

    def endpoint(**kwargs: object) -> CdpEndpoint:
        return CdpEndpoint(cdp_port, None)

    def executor(call: ToolCall) -> ToolResult:
        assert browser is not None
        return execute_browser(call, browser)

    def fixture_search(query: str, limit: int = 8) -> list[JsonObject]:
        if case is None:
            raise ValueError("controlled search requires a case")
        return search_hits(case, query, limit)

    manifest: JsonObject = {"candidate": candidate, "diagnostic": diagnostic, "guidance": guidance,
        "projection": projection, "task": task,
        "case": case.name if case is not None else "interaction_diagnostic",
        "category": case.category if case is not None else "contract",
        "model": target.model, "temperature": target.temperature,
        "connection": "cdp" if cdp_port else "headless_owned",
        "git_head": subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip(),
        "scope": "real_model_production_kernel_real_browser_controlled_document"}
    clock = fixed_request_clock() if fixed_clock else ""
    if clock:
        manifest["fixed_request_clock"] = clock
    (directory / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    try:
        if candidate:
            for name in BROWSER_TOOLS:
                spec = spec_for_tool(name)
                if spec is None:
                    raise RuntimeError(f"missing production browser spec {name}")
                if not register_custom_executor(name, executor):
                    raise RuntimeError(f"cannot register executor {name}")
        with ExitStack() as stack:
            stack.enter_context(patch.dict(os.environ, {NATIVE_TOOLS_ENV: "1"}))
            stack.enter_context(attempts.isolate_gate_secondary_models(target))
            stack.enter_context(attempts.record_api_generations(directory / "provider.jsonl"))
            if clock:
                stack.enter_context(patch("codey.providers.request_context.clock_context", return_value=clock))
            stack.enter_context(patch("codey.research.browser_search.BrowserSearchProvider._guard_request", route_document))
            stack.enter_context(patch("codey.research.source_download._open_url_no_redirect", http_document))
            stack.enter_context(patch("codey.research.search_factory.default_research_search_provider", source_factory))
            stack.enter_context(patch("codey.operations.task_phases.dispatch.default_research_search_provider", source_factory))
            if case is not None and case.name in {"policy_ranked_search", "policy_followup_search"}:
                stack.enter_context(patch.object(provider, "search", fixture_search))
            stack.enter_context(observation_candidate(projection))
            stack.enter_context(browser_action_context())
            if guidance:
                stack.enter_context(patch("codey.research.answer_context.ANSWER_GUIDANCE",
                                          answer_contract.ANSWER_GUIDANCE + "\n\n" + GUIDANCE[guidance]))
            if cdp_port:
                stack.enter_context(patch("codey.automation.browser._ensure_cdp_endpoint", endpoint))
            if candidate:
                stack.enter_context(patch.object(answer_contract, "ANSWER_TOOLS", (*answer_contract.ANSWER_TOOLS, *BROWSER_TOOLS)))
            result = run_headless(HeadlessRequest(None, task, intent="research", provider_id="local", max_turns=14,
                state_home=directory / "state", research_store_root=directory / "knowledge",
                model_selection={"model": target.model, "base_url": target.base_url}, project_changes_required=False,
                requested_capabilities=("browser.observe", "browser.interact") if candidate else ()),
                emit_jsonl=observe, connect_provider=connect)
        tools = [row for row in events if row.get("type") == "tool"]
        seen = {str(row.get("tool_name")) for row in tools if row.get("ok") is True}
        requests = [decode_json_object(json.loads(line)) for line in
                    (directory / "provider.jsonl").read_text(encoding="utf-8").splitlines()]
        delivered = any("福州样本：鱼丸净重为 173 克" in json.dumps(row.get("payload"), ensure_ascii=False)
                        for row in requests if row.get("type") == "request")
        terminal = next((row for row in reversed(events) if row.get("type") == "task_done"), None)
        answer = str(terminal.get("summary") or "") if terminal is not None else ""
        original_passages = delivered_originals(requests)
        answer_body = answer.split("\n\n## 引用来源", 1)[0]
        cited = {row["label"] for row in answer_contract.citation_occurrences(answer_body)}
        cited_originals = "\n".join(original_passages.get(label, "") for label in cited)
        quantity_in_original = all(token in cited_originals for token in expected)
        natural_success = (result.stop_reason == "done" and all(token in answer_body for token in expected)
                           and quantity_in_original and bool(cited))
        if case is not None and case.abstain:
            natural_success = (result.stop_reason == "done" and not original_passages
                               and bool(re.search(r"无法|受限|未能|限制|不可|不足|unavailable|restricted", answer_body, re.I)))
        passed = (result.stop_reason == "done" and len(navigations) == 1 and delivered
                  and diagnostic_steps_complete(tools)
                  and {"browser_snapshot", "browser_fill", "browser_click", "browser_wait", "browser_scroll"} <= seen
                  and "browser_read" in seen and quantity_in_original
                  and all(row.get("ok") is True for row in tools)) if diagnostic else natural_success
        report = canonical_json_object({**manifest, "passed": passed, "stop_reason": result.stop_reason,
            "navigations": navigations, "tools": tools, "observed_quantity_delivered": delivered,
            "answer": answer, "natural_success": natural_success,
            "quantity_in_original_passage": quantity_in_original, "original_passages": original_passages,
            "diagnostic_sequence_complete": diagnostic_steps_complete(tools) if diagnostic else None,
            "seconds": round(time.monotonic() - started, 3), "usage": model_usage(requests),
            "manual_review": case.review if case is not None else "Check conclusion and citation support.",
            "limits": "Mechanical checks plus original artifacts; conditions, exceptions and citation support require manual review."})
        (directory / "summary.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        return report
    finally:
        if candidate:
            for name in BROWSER_TOOLS:
                unregister_custom_tool(name)
        provider.close()


def delivered_originals(requests: list[JsonObject]) -> dict[str, str]:
    """Inspect actual work views; snapshot observations never count as passages."""
    passages: dict[str, str] = {}
    for row in requests:
        payload = row.get("payload")
        if row.get("type") != "request" or not isinstance(payload, dict):
            continue
        messages = payload.get("messages")
        if not isinstance(messages, list):
            continue
        for message in messages:
            content = message.get("content") if isinstance(message, dict) else None
            if not isinstance(content, str) or "Original evidence available for this answer" not in content:
                continue
            markers = list(re.finditer(r"(?m)^\[(e[1-9][0-9]*)\] [^\n]+\n", content))
            for index, marker in enumerate(markers):
                end = markers[index + 1].start() if index + 1 < len(markers) else len(content)
                passages[marker[1]] = content[marker.end():end].split("\n\n{", 1)[0].split("\n\nComplete user task", 1)[0]
    return passages


def model_usage(rows: list[JsonObject]) -> JsonObject:
    prompt, completion = 0, 0
    for row in rows:
        payload = row.get("payload")
        if row.get("type") != "response" or row.get("recorder_id") != "ui" or not isinstance(payload, dict):
            continue
        usage = payload.get("usage")
        if isinstance(usage, dict):
            input_count, output_count = usage.get("prompt_tokens"), usage.get("completion_tokens")
            prompt += input_count if type(input_count) is int else 0
            completion += output_count if type(output_count) is int else 0
    return {"prompt_tokens": prompt, "completion_tokens": completion, "cost_is_rejection_criterion": False}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-dir", type=Path, required=True)
    parser.add_argument("--timeout", type=float, default=180)
    parser.add_argument("--candidate", action="store_true")
    parser.add_argument("--diagnostic", action="store_true")
    parser.add_argument("--cdp", action="store_true", help="Use an owned test browser via real CDP")
    available = (*cases(), *policy_cases())
    parser.add_argument("--case", choices=[case.name for case in available])
    parser.add_argument("--guidance", choices=tuple(GUIDANCE), default="")
    parser.add_argument("--projection", choices=PROJECTIONS, default="")
    parser.add_argument("--fixed-clock", action="store_true")
    args = parser.parse_args()
    directory = args.run_dir.resolve()
    directory.mkdir(parents=True, exist_ok=False)
    selected_case = next((case for case in available if case.name == args.case), None)
    if args.cdp:
        with cdp_browser(directory / "browser-profile") as port:
            report = run(directory, args.timeout, candidate=args.candidate, diagnostic=args.diagnostic,
                         cdp_port=port, case=selected_case, guidance=args.guidance, projection=args.projection,
                         fixed_clock=args.fixed_clock)
    else:
        report = run(directory, args.timeout, candidate=args.candidate, diagnostic=args.diagnostic,
                     case=selected_case, guidance=args.guidance, projection=args.projection,
                     fixed_clock=args.fixed_clock)
    print(json.dumps({"passed": report["passed"], "stop_reason": report["stop_reason"]}), flush=True)
    return 0 if report["passed"] is True else 1


if __name__ == "__main__":
    raise SystemExit(main())
