"""Semantic browser diagnostics through the existing headless/provider/kernel.

Only this process registers candidate tools. Production defaults stay unchanged.
The browser executes real DOM actions; only the test document is intercepted.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import patch

from playwright.sync_api import Route

from codey.app.headless_runner import HeadlessRequest, run_headless
from codey.env_names import NATIVE_TOOLS_ENV
from codey.operations.task_execution import ExecutionDelegate
from codey.research import answer_contract
from codey.research.browser_interaction import BrowserInteraction
from codey.research.knowledge_schema import schema_object, schema_strings
from codey.research.ledger import ResearchLedger
from codey.research.resumable_browser import ResumableBrowserSearchProvider
from codey.research.source_gateway import ResearchSourceGateway
from codey.research.source_reading import render_source_read
from codey.research.tool_contract import TOOL_CONTRACTS
from codey.research.tools import ResearchTools
from codey.runtime.core.models import ToolCall, ToolResult
from codey.toolchain.tool_spec import register_custom_executor, register_custom_tool, unregister_custom_tool
from codey.types.json_value import JsonObject, canonical_json_object, decode_json_object, require_json_integer
from tools import local_model_gate_attempts as attempts
from tools.shell_web_diagnostic_gate import select_target

BROWSER_TOOLS = ("browser_snapshot", "browser_click", "browser_fill", "browser_scroll", "browser_wait", "browser_read")
URL = "https://example.com/interactive-field-record"
HTML = """<!doctype html><html><head><meta charset="utf-8"><title>Interactive record</title></head>
<body><article>Interactive field record. Enter the requested locality, then press Load.
The initial page does not contain the measured quantity.</article>
<label>Locality <input aria-label="Locality" id="query"></label>
<button id="load" onclick="window.clicks++; document.querySelector('#result').textContent =
document.querySelector('#query').value === '福州' ? '福州样本：鱼丸净重为 173 克。' : 'Unknown locality';">Load</button>
<p id="result">Awaiting locality</p><div style="height:2000px"></div>
<script>window.clicks=0;</script></body></html>"""


def execute_browser(call: ToolCall, browser: BrowserInteraction) -> ToolResult:
    """Candidate tools use the same ordinary ToolDispatch as built-in tools."""
    args = call.args
    try:
        if call.name == "browser_read":
            capture = browser.capture()
            url = str(capture.get("url") or "")
            ledger = ResearchLedger()
            outcome = ResearchSourceGateway(browser.provider, ledger).accept_capture(url, capture)
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


def run(directory: Path, timeout: float, *, candidate: bool, diagnostic: bool) -> JsonObject:
    target = select_target("gemma", timeout, "http://127.0.0.1:5001/v1")
    provider = ResumableBrowserSearchProvider(headless=True, http_fallback=False)
    browser = BrowserInteraction(provider)
    task = (f"查看 {URL} 中福州样本的鱼丸净重，说明页面实际显示的数值。"
            "只使用这个来源；如果材料不足，请如实说明。不要保存知识。")
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
        route.fulfill(status=200, content_type="text/html; charset=utf-8", body=HTML)

    def connect(provider_id: str, **kwargs: object) -> attempts.RecordingProvider:
        if provider_id != "local":
            raise ValueError("diagnostic requires the selected local model")
        return attempts.RecordingProvider(target, directory)

    def source_factory() -> ResumableBrowserSearchProvider:
        return provider

    def executor(call: ToolCall) -> ToolResult:
        return execute_browser(call, browser)

    manifest: JsonObject = {"candidate": candidate, "diagnostic": diagnostic, "task": task,
        "model": target.model, "temperature": target.temperature,
        "git_head": subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip(),
        "scope": "real_model_production_kernel_real_browser_controlled_document"}
    (directory / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    try:
        if candidate:
            for name in BROWSER_TOOLS:
                contract = TOOL_CONTRACTS[name]
                schema = canonical_json_object(contract.input_schema)
                if not register_custom_tool(name, grant="web.read", executor="browser_experiment",
                        parameters=tuple(schema_object(schema, "properties").items()),
                        required=tuple(schema_strings(schema, "required")), description=contract.description):
                    raise RuntimeError(f"cannot register candidate {name}")
                if not register_custom_executor(name, executor):
                    raise RuntimeError(f"cannot register executor {name}")
        with ExitStack() as stack:
            stack.enter_context(patch.dict(os.environ, {NATIVE_TOOLS_ENV: "1"}))
            stack.enter_context(attempts.isolate_gate_secondary_models(target))
            stack.enter_context(attempts.record_api_generations(directory / "provider.jsonl"))
            stack.enter_context(patch("codey.research.browser_search.BrowserSearchProvider._guard_request", route_document))
            stack.enter_context(patch("codey.research.search_factory.default_research_search_provider", source_factory))
            stack.enter_context(patch("codey.operations.task_phases.dispatch.default_research_search_provider", source_factory))
            if candidate:
                stack.enter_context(patch.object(answer_contract, "ANSWER_TOOLS", (*answer_contract.ANSWER_TOOLS, *BROWSER_TOOLS)))
            result = run_headless(HeadlessRequest(None, task, intent="research", provider_id="local", max_turns=14,
                state_home=directory / "state", research_store_root=directory / "knowledge",
                model_selection={"model": target.model, "base_url": target.base_url}, project_changes_required=False),
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
        quantity_in_original = any("173" in body for body in original_passages.values())
        natural_success = (result.stop_reason == "done" and "173" in answer
                           and quantity_in_original)
        passed = (result.stop_reason == "done" and len(navigations) == 1 and delivered
                  and {"browser_snapshot", "browser_fill", "browser_click", "browser_wait", "browser_scroll"} <= seen
                  and "browser_read" in seen and quantity_in_original
                  and all(row.get("ok") is True for row in tools)) if diagnostic else natural_success
        report = canonical_json_object({**manifest, "passed": passed, "stop_reason": result.stop_reason,
            "navigations": navigations, "tools": tools, "observed_quantity_delivered": delivered,
            "answer": answer, "natural_success": natural_success,
            "quantity_in_original_passage": quantity_in_original, "original_passages": original_passages,
            "limits": "Interaction contract only. Natural-task answer quality requires passage integration and review."})
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


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-dir", type=Path, required=True)
    parser.add_argument("--timeout", type=float, default=180)
    parser.add_argument("--candidate", action="store_true")
    parser.add_argument("--diagnostic", action="store_true")
    args = parser.parse_args()
    directory = args.run_dir.resolve()
    directory.mkdir(parents=True, exist_ok=False)
    report = run(directory, args.timeout, candidate=args.candidate, diagnostic=args.diagnostic)
    print(json.dumps({"passed": report["passed"], "stop_reason": report["stop_reason"]}), flush=True)
    return 0 if report["passed"] is True else 1


if __name__ == "__main__":
    raise SystemExit(main())
