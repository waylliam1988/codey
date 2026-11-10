"""Live Chat A/B: model JSON explanation versus experimental prose wrapping.

Both arms use Chromium, the production headless kernel, the same receipt
bindings and strict final schema. B only wraps done.summary at the provider
boundary; it is a candidate adapter, not an installed production capability.
No tool execution, receipt selection or completion verdict is reimplemented.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
import time
import traceback
import urllib.request
from contextlib import ExitStack
from dataclasses import dataclass
from pathlib import Path
from typing import Literal
from unittest.mock import patch

from playwright.sync_api import Route

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from codey.app.headless_runner import HeadlessRequest, run_headless
from codey.env_names import NATIVE_TOOLS_ENV
from codey.policies.output_requirement import JSON_ANSWER_GUIDANCE, evaluate
from codey.policies.tool_requirement import decode_answer, require_tool_success
from codey.providers.api_provider import ApiProvider
from codey.providers.base import ProviderToolDefinition
from codey.providers.local_discovery import probe_local_endpoint_detail
from codey.providers.local_response_codec import normalize_local_reply
from codey.research.browser_search import BrowserSearchProvider
from codey.runtime.core import cancellation
from codey.types.json_value import (
    JsonObject,
    JsonValue,
    decode_json_object,
    decode_json_value,
    require_json_integer,
    require_json_string,
)

Arm = Literal["A", "B"]
ROOT = Path(__file__).resolve().parents[1]
URL = "https://example.com/explanation-browser-record"
TOKEN = "SCHEMA_TOKEN_74"
HTML = f"""<!doctype html><html><head><meta charset="utf-8"><title>Explanation A/B</title></head><body>
<label>Locality<input id="city"></label>
<button onclick="if(document.querySelector('#city').value==='福州') {{
document.querySelector('article').textContent='福州样本：鱼丸净重为 173 克。记录校验码 QUARTZ_851。';
}}">Load record</button><article>尚未加载记录。请填写 Locality 并点击 Load record。</article>
<script>window.__contractToken='{TOKEN}';</script></body></html>"""
ACQUISITION = """unwrap(await tools.browser_navigate({url:inputs.url}));
let s=unwrap(await tools.browser_snapshot({}));
const input=s.elements.find(e=>e.name==='Locality');
unwrap(await tools.browser_fill({ref:input.ref,value:inputs.city}));
s=unwrap(await tools.browser_snapshot({}));
const button=s.elements.find(e=>e.name==='Load record');
unwrap(await tools.browser_click({ref:button.ref}));
unwrap(await tools.browser_wait({condition:'text',value:inputs.wait_text,timeout_ms:5000}));
const text=unwrap(await tools.browser_read({}));
const evaluated=unwrap(await tools.browser_eval({code:'window.__contractToken'}));
const tabs=unwrap(await tools.browser_tab_list({}));
return {text,token:evaluated.value,page_url:tabs.page_url};"""
BINDINGS = (
    require_tool_success("browser_tab_list", via="codemode", answer_pointer="/page_url", result_pointer="/page_url"),
    require_tool_success("browser_eval", via="codemode", arguments={"code": "window.__contractToken"},
                         answer_pointer="/token", result_pointer="/value"),
)
REQUIRED = ("browser_navigate", "browser_snapshot", "browser_fill", "browser_click", "browser_wait", "browser_read")
SCHEMA: JsonObject = {"type": "object", "properties": {
    name: {"type": "string", "minLength": 1} for name in ("explanation", "page_url", "token")},
    "required": ["explanation", "page_url", "token"], "additionalProperties": False}
SHAPES = (
    "用一段简短自然语言说明操作和结果。",
    "用两段文字说明观察过程和最终结果，段落之间使用真实换行。",
    '解释中用英文双引号引用按钮名 "Load record"，然后报告结果。',
    "用两条 Markdown 列表分别说明操作和结果，列表之间使用真实换行。",
    "用自然语言说明结果，并用 Markdown 行内代码提及 Locality 和 Load record。",
)
PROSE_RULE = ("Experimental explanation adapter: the application produces the final JSON. "
              "For done.summary, supply only the natural-language explanation as a string, "
              "not an inner JSON document. The adapter preserves that string at /explanation; "
              "the production assembler fills /page_url and /token from committed receipts. "
              "The required JSON schema describes the application's final output.")


def write_json(path: Path, value: object) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False), encoding="utf-8")


def append_json(path: Path, value: object) -> None:
    with path.open("a", encoding="utf-8") as stream:
        stream.write(json.dumps(value, ensure_ascii=False, allow_nan=False) + "\n")


def production_source_digest() -> str:
    tracked = subprocess.run(["git", "ls-files", "--", "codey"], cwd=ROOT, check=True,
                             capture_output=True, text=True, encoding="utf-8").stdout.splitlines()
    digest = hashlib.sha256()
    for name in tracked:
        digest.update(name.encode() + b"\0" + (ROOT / name).read_bytes())
    return digest.hexdigest()


def adapt_reply(reply: str, arm: Arm) -> tuple[str, str | None]:
    """Wrap only a valid outer done action; never repair JSON or extract prose."""
    try:
        action = decode_json_object(decode_answer(reply))
    except (ValueError, TypeError):
        return reply, None
    args = action.get("args")
    if action.get("tool") != "done" or not isinstance(args, dict) or not isinstance(args.get("summary"), str):
        return reply, None
    summary = require_json_string(args["summary"], "summary")
    if arm == "A":
        return reply, summary
    args["summary"] = json.dumps({"explanation": summary}, ensure_ascii=False, allow_nan=False)
    return json.dumps(action, ensure_ascii=False, allow_nan=False), summary


def prose_guidance(text: str) -> str:
    """Change only model-facing output guidance, retaining execution requirements."""
    text = text.replace(JSON_ANSWER_GUIDANCE, PROSE_RULE)
    for binding in BINDINGS:
        from codey.policies.tool_requirement import parse_tool_success_check

        requirement = parse_tool_success_check(binding)
        assert requirement is not None
        original = requirement.guidance.replace(JSON_ANSWER_GUIDANCE, PROSE_RULE)
        execution = original.split(PROSE_RULE, 1)[0]
        text = text.replace(original, execution + PROSE_RULE)
    return text


def service_state(base_url: str) -> JsonObject:
    try:
        with urllib.request.urlopen(base_url.removesuffix("/v1") + "/api/extra/perf", timeout=5) as response:
            return decode_json_object(json.loads(response.read()))
    except (OSError, ValueError) as exc:
        return {"error": f"{type(exc).__name__}: {exc}"}


@dataclass(frozen=True)
class Target:
    base_url: str
    model: str
    seed: int
    timeout: float


class ExperimentProvider(ApiProvider):
    def __init__(self, target: Target, directory: Path, arm: Arm, *, fixed_acquisition: bool = True) -> None:
        self.target, self.directory, self.arm = target, directory, arm
        self.exchange = 0
        self.observed_tools = 0
        self.proposals: list[JsonObject] = []
        self.fixed_acquisition = fixed_acquisition
        super().__init__(target.base_url, target.model, temperature=0, timeout=target.timeout,
                         context_window_tokens=32768, context_reserve_tokens=8192,
                         context_keep_recent_tokens=12000, native_tools=False, thinking_enabled=False,
                         text_tool_format="json_schema", text_decoder=normalize_local_reply,
                         configure_request=self.configure_experiment)

    def configure_experiment(self, payload: dict[str, object]) -> None:
        payload["seed"] = self.target.seed
        if self.arm == "B":
            messages = decode_json_value(payload.get("messages"))
            if not isinstance(messages, list):
                raise ValueError("expected chat messages")
            changed: list[JsonValue] = []
            for raw in messages:
                message = decode_json_object(raw)
                content = message.get("content")
                if message.get("role") in {"system", "user"} and isinstance(content, str):
                    message["content"] = prose_guidance(content)
                changed.append(message)
            payload["messages"] = changed

    def _generate(self, messages: list[dict[str, object]], tools: list[ProviderToolDefinition] | None = None,
                  *, timeout: float | None = None, checkpoints: list[dict[str, object]] | None = None,
                  tool_call_json: bool = False, tool_call_schema: dict[str, object] | None = None,
                  tool_call_contract: str = "", tool_call_observations: str = "",
                  tool_call_history_groups: int = 0, output_token_limit: int = 0,
                  require_tool_call: bool = False) -> dict[str, object]:
        self.exchange += 1
        if self.fixed_acquisition and self.exchange == 1:
            action = {"tool": "codemode", "args": {"code": ACQUISITION, "inputs": {
                "url": URL, "city": "福州", "wait_text": "鱼丸"}}}
            scripted_body: dict[str, object] = {"choices": [{"message": {
                "role": "assistant", "content": json.dumps(action), "tool_calls": []}, "finish_reason": "stop"}]}
            append_json(self.directory / "provider.jsonl", {"type": "scripted_acquisition",
                        "exchange": self.exchange, "payload": scripted_body})
            return scripted_body
        started = time.monotonic()
        try:
            body: object = super()._generate(
                messages, tools, timeout=timeout, checkpoints=checkpoints, tool_call_json=tool_call_json,
                tool_call_schema=tool_call_schema, tool_call_contract=tool_call_contract,
                tool_call_observations=tool_call_observations, tool_call_history_groups=tool_call_history_groups,
                output_token_limit=output_token_limit, require_tool_call=require_tool_call)
            result = decode_json_object(body)
            append_json(self.directory / "provider.jsonl", {"type": "response", "exchange": self.exchange,
                        "payload": result, "seconds": time.monotonic() - started,
                        "service": service_state(self.target.base_url)})
            return dict(result)
        except Exception as exc:
            append_json(self.directory / "provider.jsonl", {"type": "error", "exchange": self.exchange,
                        "error": f"{type(exc).__name__}: {exc}", "seconds": time.monotonic() - started})
            raise

    def _observe_http_attempt(self, *, attempt: int, data: bytes, phase: str,
                              response_bytes: int, seconds: float) -> None:
        row: JsonObject = {"exchange": self.exchange, "type": "wire_attempt", "phase": phase,
                           "attempt": attempt, "request_sha256": hashlib.sha256(data).hexdigest(),
                           "response_bytes": response_bytes, "seconds": seconds}
        if phase == "request":
            row["payload"] = decode_json_object(json.loads(data))
        append_json(self.directory / "provider.jsonl", row)

    def normalize_reply(self, reply: str) -> object:
        normalized = super().normalize_reply(reply)
        if not isinstance(normalized, str):
            return normalized
        adapted, summary = adapt_reply(normalized, self.arm)
        if summary is not None:
            row: JsonObject = {"exchange": self.exchange, "raw_reply": reply,
                               "normalized_reply": normalized, "adapted_reply": adapted, "raw_summary": summary,
                               "tools_before_proposal": self.observed_tools}
            if not self.proposals or self.proposals[-1]["exchange"] != self.exchange:
                self.proposals.append(row)
                append_json(self.directory / "proposals.jsonl", row)
        return adapted


def final_checks(answer: str) -> JsonObject:
    """Field equality is independent of other correct strings elsewhere in prose."""
    from codey.policies.output_requirement import declaration

    valid, reason, _feedback = evaluate(declaration("json", json.dumps(SCHEMA)), answer)
    if not valid:
        return {"schema_valid": False, "exact_fields": False, "facts_mentioned": False,
                "explanation": None, "reason": reason}
    value = decode_json_object(decode_answer(answer))
    explanation = require_json_string(value["explanation"], "explanation")
    return {"schema_valid": True,
            "exact_fields": value["page_url"] == URL and value["token"] == TOKEN,
            "facts_mentioned": bool(re.search(r"(?<!\d)173(?!\d)", explanation))
            and bool(re.search(r"(?<![A-Za-z0-9_])QUARTZ_851(?![A-Za-z0-9_])", explanation)),
            "explanation": explanation, "reason": ""}


def provider_metrics(path: Path) -> JsonObject:
    rows = [decode_json_object(json.loads(line)) for line in path.read_text(encoding="utf-8").splitlines()
            ] if path.exists() else []
    requests = {require_json_integer(row["exchange"], "exchange") for row in rows
                if row["type"] == "wire_attempt" and row.get("phase") == "request"}
    responses = [row for row in rows if row["type"] == "response"]
    usage: list[JsonObject] = []
    for row in responses:
        payload = row["payload"]
        if isinstance(payload, dict) and isinstance(payload.get("usage"), dict):
            usage.append(decode_json_object(payload["usage"]))
    complete = len(responses) == len(requests) == len(usage)
    result: JsonObject = {"model_calls": len(requests), "usage_complete": complete,
                          "inference_seconds": round(sum(float(str(row["seconds"])) for row in responses), 3)}
    for key in ("prompt_tokens", "completion_tokens"):
        values = [row.get(key) for row in usage]
        result[key] = sum(require_json_integer(value, key) for value in values) if complete and all(
            type(value) is int for value in values) else None
    return result


def run_arm(directory: Path, target: Target, arm: Arm, shape: int, max_turns: int,
            *, fixed_acquisition: bool = True) -> JsonObject:
    directory.mkdir(parents=True, exist_ok=False)
    browser = BrowserSearchProvider(headless=True)
    provider = ExperimentProvider(target, directory, arm, fixed_acquisition=fixed_acquisition)
    events: list[JsonObject] = []
    started = time.monotonic()

    def observe(row: dict[str, object]) -> None:
        event = decode_json_object(row)
        events.append(event)
        append_json(directory / "events.jsonl", event)
        if event.get("type") == "tool":
            provider.observed_tools += 1

    def document(route: Route) -> None:
        route.fulfill(status=200 if route.request.url == URL else 404, content_type="text/html",
                      body=HTML if route.request.url == URL else "Fixture URL unavailable")

    def connect(provider_id: str, **_kwargs: object) -> ExperimentProvider:
        if provider_id != "local":
            raise ValueError("experiment only permits the pinned local model")
        return provider

    task = (f"用户授权浏览器任务：用 codemode 顺序操作 {URL}。"
            "browser_navigate 后 browser_snapshot，找到 Locality 输入框并 browser_fill 填入福州；"
            "填写后重新 browser_snapshot 获取新的 ref，browser_click 点击 Load record，"
            "browser_wait 等待文本鱼丸出现，然后 browser_read 读取正文。"
            "用 unwrap 获取结果；browser_read 的值是字符串，不要再取 .text。"
            "再用 browser_eval，参数 code 必须是 window.__contractToken；用 browser_tab_list 获取 URL。"
            "每个 CodeMode 程序显式 return，检查结果，不猜 ref，不运行命令或测试。"
            "最后通过 done 提交解释，用数字报告实测净重和校验码；不要在解释中重复 URL 或脚本 token。"
            + SHAPES[shape] + "URL 和 token 均由应用从正式回执填入。")
    task += ('done.summary 只提交一个 JSON 对象，形如 {"explanation":"你的解释"}，不抄写其他字段。'
             if arm == "A" else "实验适配已启用：done.summary 直接填写自然语言解释，不包装成内层 JSON 对象。")
    if fixed_acquisition:
        task += "实验固定采集程序将先执行上述浏览器步骤；成功回执到达后直接提交解释，不要重复已经成功的操作。"
    write_json(directory / "request.json", {"task": task, "arm": arm, "shape": shape,
               "required_tools": [*REQUIRED, *BINDINGS], "output_schema": SCHEMA, "seed": target.seed,
               "fixed_acquisition": fixed_acquisition})
    summary: JsonObject = {"arm": arm, "shape": shape, "passed": False,
                           "scope": "live_model_and_chromium_with_experimental_provider_boundary_adapter",
                           "fixed_acquisition": fixed_acquisition}
    try:
        with ExitStack() as stack:
            stack.enter_context(patch.dict(os.environ, {NATIVE_TOOLS_ENV: "1"}))
            stack.enter_context(patch("codey.app.provider_services.provider_availability", return_value={"local": True}))
            stack.enter_context(patch.object(browser, "_guard_request", document))
            stack.enter_context(patch("codey.research.search_factory.default_research_search_provider",
                                      return_value=browser))
            stack.enter_context(patch("codey.operations.task_phases.dispatch.default_research_search_provider",
                                      return_value=browser))
            result = run_headless(HeadlessRequest(
                None, task, intent="chat", provider_id="local", max_turns=max_turns,
                state_home=directory / "state", research_store_root=directory / "knowledge",
                requested_capabilities=("web.read", "browser.observe", "browser.interact", "browser.effect"),
                required_tools=(*REQUIRED, *BINDINGS), output_format="json", output_schema=json.dumps(SCHEMA),
                model_selection={"model": target.model, "base_url": target.base_url}),
                emit_jsonl=observe, connect_provider=connect)
        terminal = next((row for row in reversed(events) if row.get("type") == "task_done"), {})
        answer = str(terminal.get("summary") or "")
        checks = final_checks(answer)
        tools = [row for row in events if row.get("type") == "tool"]
        successful = {str(row["tool_name"]) for row in tools if row.get("ok") is True}
        expected = {*REQUIRED, "browser_eval", "browser_tab_list"}
        passed = (result.stop_reason == "done" and expected <= successful
                  and all(checks[key] is True for key in ("schema_valid", "exact_fields", "facts_mentioned")))
        names: list[JsonValue] = [name for name in sorted(successful)]
        summary.update(checks, passed=passed, stop_reason=result.stop_reason, answer=answer,
                       successful_tools=names, required_executions_present=expected <= successful)
    except Exception as exc:
        summary.update(error=f"{type(exc).__name__}: {exc}")
        (directory / "error.txt").write_text(traceback.format_exc(), encoding="utf-8")
    finally:
        browser.close()
        provider.close()
    proposals = provider.proposals
    tools = [row for row in events if row.get("type") == "tool"]
    first_exchange = require_json_integer(proposals[0]["exchange"], "exchange") if proposals else None
    tools_before = require_json_integer(proposals[0]["tools_before_proposal"], "tools_before_proposal") if proposals else None
    metrics = provider_metrics(directory / "provider.jsonl")
    first_success = summary["passed"] is True and len(proposals) == 1
    if fixed_acquisition:
        first_success = first_success and first_exchange == 2 and metrics["model_calls"] == 1
    summary.update(seconds=round(time.monotonic() - started, 3), done_proposals=len(proposals),
                   first_delivery_success=first_success,
                   correction_proposals=max(0, len(proposals) - 1),
                   tools_after_first_done=len(tools) - tools_before if tools_before is not None else None,
                   first_done_exchange=first_exchange,
                   failed_tools=sum(row.get("ok") is False for row in tools), **metrics)
    summary["failure_stage"] = ("none" if summary["passed"] is True else "before_delivery" if not proposals
                                else "delivery_or_final_validation")
    write_json(directory / "summary.json", summary)
    return summary


def run_worker(directory: Path, target: Target, arm: Arm, shape: int, max_turns: int, budget: float,
               *, fixed_acquisition: bool = True) -> JsonObject:
    command = [sys.executable, "-m", "tools.completion_explanation_ab", "--run-dir", str(directory),
               "--worker-arm", arm, "--worker-shape", str(shape), "--model", target.model,
               "--base-url", target.base_url, "--seed", str(target.seed), "--timeout", str(target.timeout),
               "--max-turns", str(max_turns)]
    if not fixed_acquisition:
        command.append("--autonomous-acquisition")
    process, owner = cancellation.start_process(command, cwd=ROOT)
    try:
        result = cancellation.wait_process(process, owner, command, budget, capture_limit_bytes=100_000)
        output = result.stdout + result.stderr
        error = f"worker exited {result.returncode}" if result.returncode else ""
    except subprocess.TimeoutExpired:
        output, error = "", f"worker exceeded {budget}s; owned process tree terminated"
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "worker.log").write_text(output + error, encoding="utf-8")
    path = directory / "summary.json"
    if path.exists() and not error:
        return decode_json_object(json.loads(path.read_text(encoding="utf-8")))
    row: JsonObject = {"arm": arm, "shape": shape, "passed": False, "first_delivery_success": False,
                       "error": error, "failure_class": "environment_or_worker_failure"}
    write_json(path, row)
    return row


def aggregate(rows: list[JsonObject], arm: Arm) -> JsonObject:
    selected = [row for row in rows if row["arm"] == arm]
    result: JsonObject = {"runs": len(selected), "passed": sum(row.get("passed") is True for row in selected),
                          "first_delivery_successes": sum(row.get("first_delivery_success") is True for row in selected),
                          "reached_delivery": sum(isinstance(row.get("done_proposals"), int)
                                                  and bool(row["done_proposals"]) for row in selected)}
    for key in ("model_calls", "prompt_tokens", "completion_tokens", "done_proposals", "failed_tools"):
        values = [row.get(key) for row in selected]
        result[key] = sum(require_json_integer(v, key) for v in values) if all(type(v) is int for v in values) else None
    for key in ("seconds", "inference_seconds"):
        values = [row.get(key) for row in selected]
        result[key] = round(sum(float(str(v)) for v in values), 3) if all(
            isinstance(v, (int, float)) for v in values) else None
    return result


def explanation_checks(explanation: str, shape: int) -> JsonObject:
    """Limited presentation audit; never a claim of general semantic correctness."""
    list_items = len(re.findall(r"(?m)^(?:[-*]|\d+[.)])\s+", explanation))
    shape_valid = (
        "\n\n" not in explanation,
        "\n\n" in explanation,
        '"Load record"' in explanation,
        list_items == 2,
        "`Locality`" in explanation and "`Load record`" in explanation,
    )[shape]
    urls = [value.rstrip(".,;:!?)") for value in re.findall(r'https?://[^\s`<>"，。；、！？）]+', explanation)]
    tokens = re.findall(r"SCHEMA_TOKEN_[A-Za-z0-9_]+", explanation)
    return {"requested_shape_valid": shape_valid, "literal_backslash_n": "\\n" in explanation,
            "bound_values_repeated": bool(urls or tokens),
            "unexpected_url_mentions": [value for value in urls if value != URL],
            "unexpected_token_mentions": [value for value in tokens if value != TOKEN]}


def audit_run(directory: Path) -> JsonObject:
    """Read original artifacts and write a separate audit, leaving primary results intact."""
    report = decode_json_object(json.loads((directory / "summary.json").read_text(encoding="utf-8")))
    manifest = decode_json_object(json.loads((directory / "manifest.json").read_text(encoding="utf-8")))
    if manifest.get("interface") != "explanation-wrapper-ab-v1":
        raise ValueError("unsupported experiment artifact version")
    results = report["results"]
    if not isinstance(results, list):
        raise ValueError("expected experiment result rows")
    cases: list[JsonValue] = []
    seeds: list[int] = []
    totals: list[int] = []
    missing_service = 0
    for value in results:
        row = decode_json_object(value)
        path = directory / f"{require_json_integer(row['pair'], 'pair'):02d}" / require_json_string(row["arm"], "arm")
        proposals_path = path / "proposals.jsonl"
        proposals = [decode_json_object(json.loads(line)) for line in proposals_path.read_text(encoding="utf-8").splitlines()
                     ] if proposals_path.exists() else []
        case: JsonObject = {"pair": row["pair"], "arm": row["arm"], "primary_passed": row["passed"],
                            "published": row.get("stop_reason") == "done", "raw_explanation_preserved": None,
                            "first_inner_json_error": None,
                            "failure_class": "none" if row["passed"] is True else "requires_manual_review"}
        if proposals:
            raw = require_json_string(proposals[0]["raw_summary"], "raw_summary")
            if row["arm"] == "A":
                try:
                    first = decode_json_object(decode_answer(raw))
                    require_json_string(first["explanation"], "explanation")
                except (ValueError, TypeError, KeyError) as exc:
                    case["first_inner_json_error"] = str(exc)
                    if row["passed"] is False and row.get("required_executions_present") is True:
                        case["failure_class"] = "model_inner_json_encoding_or_packaging"
            if case["published"] is True:
                final = decode_json_object(decode_answer(require_json_string(row["answer"], "answer")))
                explanation = require_json_string(final["explanation"], "explanation")
                last_raw = require_json_string(proposals[-1]["raw_summary"], "raw_summary")
                original = (require_json_string(decode_json_object(decode_answer(last_raw))["explanation"], "explanation")
                            if row["arm"] == "A" else last_raw)
                case["raw_explanation_preserved"] = explanation == original
                case.update(explanation_checks(explanation, require_json_integer(row["shape"], "shape")))
        cases.append(case)
        provider_path = path / "provider.jsonl"
        for line in provider_path.read_text(encoding="utf-8").splitlines() if provider_path.exists() else ():
            response = decode_json_object(json.loads(line))
            if response["type"] != "response":
                continue
            service = response.get("service")
            if isinstance(service, dict) and type(service.get("last_seed")) is int:
                seeds.append(require_json_integer(service["last_seed"], "last_seed"))
                totals.append(require_json_integer(service["total_gens"], "total_gens"))
            else:
                missing_service += 1
    audited: JsonObject = {"cases": cases, "service_observations": len(seeds),
                           "missing_service_observations": missing_service,
                           "all_observed_seeds_match": bool(seeds) and all(seed == manifest["seed"] for seed in seeds),
                           "service_generation_counts_consecutive": bool(totals)
                           and all(b == a + 1 for a, b in zip(totals, totals[1:], strict=False)),
                           "production_unchanged": report["source_before"] == report["source_after"]
                           == production_source_digest(),
                           "limits": "Presentation patterns and fact markers are bounded checks, not a full semantic oracle."}
    for arm in ("A", "B"):
        selected = [decode_json_object(value) for value in cases if decode_json_object(value)["arm"] == arm]
        published = [case for case in selected if case["published"] is True]
        audited[arm] = {
            "runs": len(selected), "published": len(published),
            "raw_explanations_preserved": sum(case["raw_explanation_preserved"] is True for case in published),
            "first_inner_json_errors": sum(case["first_inner_json_error"] is not None for case in selected),
            "published_requested_shape_passes": sum(case.get("requested_shape_valid") is True for case in published),
            "published_bound_values_repeated": sum(case.get("bound_values_repeated") is True for case in published),
            "published_unexpected_urls_or_tokens": sum(bool(case.get("unexpected_url_mentions"))
                                                       or bool(case.get("unexpected_token_mentions")) for case in published),
        }
    write_json(directory / "audit.json", audited)
    return audited


@dataclass
class Options:
    run_dir: Path = ROOT
    pairs: int = 10
    base_url: str = "http://127.0.0.1:5001/v1"
    model: str = ""
    seed: int = 123456789
    timeout: float = 180
    case_budget: float = 360
    max_turns: int = 12
    worker_arm: str | None = None
    worker_shape: int = 0
    autonomous_acquisition: bool = False
    audit: bool = False


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-dir", required=True, type=Path)
    parser.add_argument("--pairs", type=int, default=10)
    parser.add_argument("--base-url", default="http://127.0.0.1:5001/v1")
    parser.add_argument("--model", default="")
    parser.add_argument("--seed", type=int, default=123456789)
    parser.add_argument("--timeout", type=float, default=180)
    parser.add_argument("--case-budget", type=float, default=360)
    parser.add_argument("--max-turns", type=int, default=12)
    parser.add_argument("--worker-arm", choices=("A", "B"))
    parser.add_argument("--worker-shape", type=int, default=0)
    parser.add_argument("--autonomous-acquisition", action="store_true",
                        help="Exploratory full-task run; default fixes acquisition to isolate final delivery.")
    parser.add_argument("--audit", action="store_true", help="Audit existing artifacts without calling a model.")
    args = parser.parse_args(namespace=Options())
    directory = args.run_dir.resolve()
    if args.audit:
        print(json.dumps(audit_run(directory), ensure_ascii=True, indent=2))
        return 0
    base_url = args.base_url.rstrip("/")
    model = args.model
    if not model:
        endpoint, reason = probe_local_endpoint_detail(base_url, timeout=5)
        if endpoint is None or not endpoint.models:
            parser.error("local model unavailable: " + reason)
        model = endpoint.models[0]
    target = Target(base_url, model, args.seed, args.timeout)
    if args.worker_arm:
        arm: Arm = "A" if args.worker_arm == "A" else "B"
        print(json.dumps(run_arm(directory, target, arm, args.worker_shape, args.max_turns,
                                 fixed_acquisition=not args.autonomous_acquisition),
                         ensure_ascii=True), flush=True)
        return 0
    if args.pairs < 1:
        parser.error("--pairs must be positive")
    directory.mkdir(parents=True, exist_ok=False)
    before = production_source_digest()
    write_json(directory / "manifest.json", {"interface": "explanation-wrapper-ab-v1", "model": model,
               "base_url": base_url, "seed": target.seed, "temperature": 0, "pairs": args.pairs,
               "scope": "chat_browser_task_and_candidate_explanation_adapter",
               "fixed_acquisition": not args.autonomous_acquisition,
               "source_before": before, "schema": SCHEMA, "bindings": list(BINDINGS),
               "order": "AB for even pairs; BA for odd pairs; five explanation shapes repeated",
               "limits": "Fixed seed does not guarantee backend determinism; fact mentions are not full semantic validation."})
    rows: list[JsonObject] = []
    for pair in range(args.pairs):
        order: tuple[Arm, Arm] = ("A", "B") if pair % 2 == 0 else ("B", "A")
        for arm in order:
            print(json.dumps({"starting_pair": pair + 1, "arm": arm}), flush=True)
            row = run_worker(directory / f"{pair + 1:02d}" / arm, target, arm, pair % len(SHAPES),
                             args.max_turns, args.case_budget, fixed_acquisition=not args.autonomous_acquisition)
            row["pair"] = pair + 1
            rows.append(row)
            append_json(directory / "results.jsonl", row)
            report: JsonObject = {"A": aggregate(rows, "A"), "B": aggregate(rows, "B"),
                                  "results": list(rows), "source_before": before,
                                  "source_after": production_source_digest()}
            write_json(directory / "summary.json", report)
            print(json.dumps({k: row.get(k) for k in ("pair", "arm", "passed", "stop_reason",
                             "done_proposals", "model_calls", "error")}, ensure_ascii=True), flush=True)
    return 0 if before == production_source_digest() else 1


if __name__ == "__main__":
    raise SystemExit(main())
