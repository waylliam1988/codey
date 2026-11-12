"""Experimental evidence assessment and submission for natural Research requests."""
from __future__ import annotations

import argparse
import hashlib
import json
from collections.abc import Callable, Generator
from contextlib import contextmanager
from dataclasses import dataclass, replace
from pathlib import Path
from typing import cast
from unittest.mock import patch

from playwright.sync_api import Route

from codey.app.headless_runner import HeadlessRequest, run_headless
from codey.operations import kernel_protocol
from codey.operations.browser_completion import committed_results, complete_page_passages
from codey.operations.browser_upload import browser_opened
from codey.operations.kernel_context import KernelContext
from codey.operations.kernel_protocol import TurnSnapshot
from codey.operations.task_execution import ExecutionDelegate
from codey.policies.browser_requirement import PAGE_TRANSITIONS
from codey.policies.tool_requirement import _browser_body
from codey.protocols.text_turn import DecodedTextTurn, TextError, TextRequest, TextRequests
from codey.providers.base import AssistantTurn, ProviderToolResult
from codey.providers.text_result_views import TextResultSpan
from codey.research.answer_contract import AnswerScope
from codey.research.browser_search import BrowserSearchProvider
from codey.research.tools import ResearchTools
from codey.runtime.core.models import ToolCall, ToolPlan, ToolResult
from codey.runtime.core.operation_payload import RuntimeExecutionCounters, RuntimeOperationState
from codey.toolchain.tool_spec import ToolSpec, freeze_spec_parameters, json_contract_text, validate_args_with_spec
from codey.types.json_value import JsonObject, decode_json_object
from tools.browser_production_live_gate import select_target
from tools.completion_artifact_audit import tool_receipts
from tools.completion_explanation_ab import (
    Arm,
    ExperimentProvider,
    Target,
    append_json,
    production_source_digest,
    provider_metrics,
    write_json,
)
from tools.research_submission_handoff_ab import Handoff, narrow

BASE = "https://example.com"
ASSESS = "Assess which facts in the current evidence answer the user's question."
PROGRAM = "return inputs.coverage;"
READS = frozenset({"open_url", "browser_read"})
CHANGES = PAGE_TRANSITIONS | {"browser_click", "browser_fill"}


@dataclass(frozen=True)
class Case:
    path: str
    question: str
    html: str
    facts: tuple[str, ...]

    @property
    def url(self) -> str:
        return BASE + self.path


CASES = {
    "visible": Case("/library/LY-72", "帮我看看这个页面，LY-72 还有几本可借，什么时候关门？",
        "<h1>馆藏 LY-72</h1><p>可借数量：6 本</p><p>闭馆时间：19:40</p>", ("6 本", "19:40")),
    "interactive": Case("/warehouse/B-19", "帮我看看 B-19 这批货现在是什么状态，在哪个库位？",
        "<h1>批次 B-19</h1><article>实时记录尚未加载</article>"
        "<button onclick=\"setTimeout(()=>document.querySelector('article').textContent='B-19 状态：已入库。库位：C-42。',850)\">"
        "查实时记录</button>", ("已入库", "C-42")),
    "missing": Case("/orders/A-173", "帮我看看 A-173 这单到哪了，预计什么时候送到？",
        "<h1>订单 A-173</h1><p>当前状态：运输中</p><article>预计送达时间未展开</article>"
        "<button onclick=\"document.querySelector('article').textContent='A-173 预计送达：周五 18:20。'\">查看配送详情</button>"
        "<aside>其他订单 A-174，预计周四 12:10 送达</aside>", ("运输中", "周五 18:20")),
}


@dataclass(frozen=True)
class Evidence:
    key: str
    texts: dict[str, str]


def assess_evidence(scope: AnswerScope, rows: list[tuple[str, ToolResult]], document: JsonObject | None) -> Evidence | None:
    if document is None or not isinstance(url := document.get("url"), str):
        return None
    texts: dict[str, str] = {}
    for _, result in rows:
        if result.call.name in CHANGES:
            texts.clear()
        if result.call.name in READS and result.audit.get("browser_document") == document:
            passages = complete_page_passages(result, url)
            if passages and (body := _browser_body(result)) is not None:
                texts = {label: body for label, ref in scope.aliases if ref in passages}
    if not texts:
        return None
    digest = hashlib.sha256(json.dumps([document, texts], sort_keys=True).encode()).hexdigest()
    return Evidence(digest, texts)


def decision(evidence: Evidence, raw: object) -> str:
    row = decode_json_object(raw)
    quotes = row.get("quotes")
    if not isinstance(quotes, list):
        return "invalid"
    for item in quotes:
        quote = decode_json_object(item)
        label, excerpt = quote.get("label"), quote.get("text")
        if (not isinstance(label, str) or not isinstance(excerpt, str) or not excerpt.strip()
                or excerpt not in evidence.texts.get(label, "")):
            return "invalid"
    if row.get("state") == "missing" and isinstance(gap := row.get("missing"), str) and gap.strip():
        return "missing"
    return "ready" if row.get("state") == "ready" and row.get("missing") == "" and quotes else "invalid"


def assessment_spec(spec: ToolSpec) -> ToolSpec:
    capture = decode_json_object(json.loads(spec.declarations))
    quote = {"type": "object", "additionalProperties": False, "required": ["label", "text"], "properties": {
        "label": {"type": "string", "enum": capture["labels"]}, "text": {"type": "string"}}}
    coverage = {"type": "object", "additionalProperties": False, "required": ["state", "missing", "quotes"], "properties": {
        "state": {"type": "string", "enum": ["ready", "missing"]}, "missing": {"type": "string"},
        "quotes": {"type": "array", "items": quote, "maxItems": 8}}}
    return freeze_spec_parameters(replace(spec, parameters=(("coverage", coverage),), required=("coverage",),
        declarations="", json_examples=()))


def assessment_snapshot(snapshot: TurnSnapshot, evidence: Evidence) -> TurnSnapshot:
    spec = next(spec for spec in snapshot.frozen_specs if spec.name == "codemode")
    spec = replace(spec, description=ASSESS, declarations=json.dumps({"key": evidence.key, "labels": list(evidence.texts)}))
    return replace(snapshot, tool_names=("codemode",), nested_tool_names=(), frozen_specs=(spec,),
        custom_executors=tuple(row for row in snapshot.custom_executors if row[0] == "codemode"),
        contract_text=json_contract_text((assessment_spec(spec),)))


def acquisition_snapshot(snapshot: TurnSnapshot) -> TurnSnapshot:
    removed = "submit_research_answer"
    return replace(snapshot, tool_names=tuple(name for name in snapshot.tool_names if name != removed),
        frozen_specs=tuple(spec for spec in snapshot.frozen_specs if spec.name != removed),
        custom_executors=tuple(row for row in snapshot.custom_executors if row[0] != removed),
        contract_text=json_contract_text(tuple(spec for spec in snapshot.model_specs if spec.name != removed)))


@contextmanager
def experiment(directory: Path, arm: Arm) -> Generator[None, None, None]:
    original_freeze, normalize = KernelContext.freeze, kernel_protocol.normalize_turn
    descriptor: object = vars(TurnSnapshot)["model_specs"]
    assert isinstance(descriptor, property) and descriptor.fget is not None
    original_specs = cast("Callable[[TurnSnapshot], tuple[ToolSpec, ...]]", descriptor.fget)
    execute_browser, open_source = ExecutionDelegate._execute_browser, ExecutionDelegate._execute_source_open

    def specs(snapshot: TurnSnapshot) -> tuple[ToolSpec, ...]:
        return tuple(assessment_spec(spec) if spec.description == ASSESS else spec for spec in original_specs(snapshot))

    def lower(reply: DecodedTextTurn | AssistantTurn, *, snapshot: TurnSnapshot) -> ToolPlan:
        if isinstance(reply, TextRequests) and len(reply.items) == 1:
            item = reply.items[0]
            spec = next((spec for spec in snapshot.frozen_specs if spec.description == ASSESS), None)
            if spec is not None and item.tool == "codemode":
                error = validate_args_with_spec(assessment_spec(spec), item.args)
                if error:
                    return normalize(TextError(error, tool="codemode"), snapshot=snapshot)
                capture = decode_json_object(json.loads(spec.declarations))
                reply = TextRequests((TextRequest("codemode", {"code": PROGRAM,
                    "inputs": {"coverage": item.args["coverage"], "evidence_key": capture["key"]}}),))
        return normalize(reply, snapshot=snapshot)

    def read(delegate: ExecutionDelegate, call: ToolCall, *, turn: int, tool_index: int) -> tuple[ToolResult, bool, int | None]:
        browser = delegate.browser_session
        before = browser.document_identity() if browser is not None and call.name == "browser_read" else None
        result, ok, code = execute_browser(delegate, call, turn=turn, tool_index=tool_index)
        if ok and before is not None and browser is not None and browser.document_identity() == before:
            result = replace(result, audit={**result.audit, "browser_document": before})
        return result, ok, code

    def opened(delegate: ExecutionDelegate, call: ToolCall, tools: ResearchTools, *, turn: int,
               tool_index: int) -> tuple[ToolResult, bool, int | None]:
        result, ok, code = open_source(delegate, call, tools, turn=turn, tool_index=tool_index)
        browser = delegate.browser_session
        if ok and call.name == "open_url" and browser is not None and isinstance(url := call.args.get("url"), str):
            document = browser.document_identity()
            if document is not None and document.get("url") == url and browser_opened(result, url):
                result = replace(result, audit={**result.audit, "browser_document": document})
        return result, ok, code

    def freeze(context: KernelContext, state: RuntimeOperationState, snapshot: TurnSnapshot, *, turn: int,
               prompt: str = "", messages: list[ProviderToolResult] | None = None, acknowledge: bool = False,
               counters: RuntimeExecutionCounters | None = None, text_result_spans: tuple[TextResultSpan, ...] = (),
               coding_context_prepared: bool = False, initial_source_batch: bool = False) -> None:
        scope = snapshot.research_scope
        if isinstance(scope, AnswerScope) and not acknowledge:
            tools = context.execution.research_tools
            browser = tools.browser_session if tools is not None else None
            results = context.session._memory_results
            rows = committed_results(context.session.executed, results, READS | CHANGES | {"codemode"})
            evidence = assess_evidence(scope, rows, browser.document_identity() if browser is not None else None)
            phase, status, hint = "collect", "", ""
            checked: object = None
            if evidence is not None and arm == "B":
                pending_acquisition = True
                for _, result in reversed(rows):
                    inputs = result.call.args.get("inputs")
                    if (result.call.name == "codemode" and result.call.args.get("code") == PROGRAM
                            and isinstance(inputs, dict) and inputs.get("evidence_key") == evidence.key):
                        checked = inputs.get("coverage")
                        status = decision(evidence, checked)
                        break
                    if result.call.name != "codemode" or result.call.args.get("code") != PROGRAM:
                        pending_acquisition = False
                labels = ", ".join(f"[{label}]" for label in evidence.texts)
                if status == "ready":
                    phase = "submit"
                    snapshot = narrow(snapshot, Handoff("submit", "verified_quotes", tuple(evidence.texts)))
                    hint = f"Use submit_research_answer and cite the current evidence {labels}. Answer the user's whole question."
                elif status == "missing":
                    gap = decode_json_object(checked)["missing"]
                    hint = f"Continue gathering evidence for this part of the user's question: {gap}. Keep the evidence already found."
                    if pending_acquisition:
                        snapshot = acquisition_snapshot(snapshot)
                        hint += " Look for a visible control or link that can reveal the missing details."
                else:
                    phase = "assess"
                    snapshot = assessment_snapshot(snapshot, evidence)
                    hint = ("Check every requested fact against the current page evidence " + labels + ". Use codemode with coverage. "
                        "If any fact is missing, report state=missing and the gap. Quotes of partial evidence are optional. If all facts are present, "
                        "report state=ready, missing empty, and one short exact quote per fact with its evidence label. "
                        "Exclude other records, placeholders and guesses. Labels in the quote object omit brackets.")
                    if status == "invalid":
                        hint += " The previous quotes did not match the current evidence."
            append_json(directory / "handoff.jsonl", {"turn": turn, "phase": phase, "status": status, "assessment": checked,
                "evidence": evidence.texts if evidence is not None else {}, "tools": list(snapshot.tool_names)})
            if hint:
                prompt += "\n\n" + hint
        original_freeze(context, state, snapshot, turn=turn, prompt=prompt, messages=messages, acknowledge=acknowledge,
            counters=counters, text_result_spans=text_result_spans, coding_context_prepared=coding_context_prepared,
            initial_source_batch=initial_source_batch)

    with (patch.object(ExecutionDelegate, "_execute_browser", read), patch.object(ExecutionDelegate, "_execute_source_open", opened),
          patch.object(TurnSnapshot, "model_specs", property(specs)), patch.object(kernel_protocol, "normalize_turn", lower),
          patch.object(KernelContext, "freeze", freeze)):
        yield


def contains_fact(text: str, fact: str) -> bool:
    return "".join(fact.split()) in "".join(text.split())


def cited_page_text(receipts: list[ToolResult], case: Case) -> str:
    submitted = next((row.canonical.get("research_submission") for row in reversed(receipts)
                      if row.ok and row.call.name == "submit_research_answer"), None)
    citations = submitted.get("citations") if isinstance(submitted, dict) else None
    cited = {row.get("passage_ref") for row in citations if isinstance(row, dict)} if isinstance(citations, list) else set()
    return "\n".join((_browser_body(row) or "") for row in receipts if row.ok and row.call.name in READS
                     and cited.intersection(complete_page_passages(row, case.url)))


def run_arm(directory: Path, case: Case, arm: Arm, target: Target, *, max_turns: int = 10,
            separate_url: bool = False) -> JsonObject:
    before = production_source_digest()
    directory.mkdir(parents=True, exist_ok=False)
    browser = BrowserSearchProvider(headless=True)
    provider = ExperimentProvider(target, directory, fixed_acquisition=False)
    events: list[JsonObject] = []

    def route(request: Route) -> None:
        matching = request.request.url == case.url
        request.fulfill(status=200 if matching else 404, content_type="text/html",
            body=f'<html><head><meta charset="utf-8"></head><body>{case.html}</body></html>' if matching else "Fixture unavailable")

    def observe(event: dict[str, object]) -> None:
        value = decode_json_object(event)
        events.append(value)
        append_json(directory / "events.jsonl", value)

    def connect(_provider_id: str, **_kwargs: object) -> ExperimentProvider:
        return provider

    task = f"{case.question}\n{case.url}" if separate_url else f"帮我看看 {case.url}，{case.question.removeprefix('帮我看看')}"
    write_json(directory / "input.json", {"task": task, "model": target.model, "seed": target.seed, "max_turns": max_turns})
    try:
        with (experiment(directory, arm), patch.object(browser, "_guard_request", route),
              patch("codey.research.search_factory.default_research_search_provider", return_value=browser),
              patch("codey.operations.task_phases.dispatch.default_research_search_provider", return_value=browser),
              patch("codey.app.provider_services.provider_availability", return_value={"local": True}),
              patch("codey.app.provider_services.provider_tab_availability", return_value={}),
              patch("codey.app.review_service.run_review", return_value=None)):
            result = run_headless(HeadlessRequest(None, task, intent="research", strict_research=True, provider_id="local",
                max_turns=max_turns, state_home=directory / "state", research_store_root=directory / "knowledge",
                requested_capabilities=("web.read", "browser.observe", "browser.interact"),
                model_selection={"model": target.model, "base_url": target.base_url}), emit_jsonl=observe, connect_provider=connect)
    finally:
        browser.close()
        provider.close()
    receipts = [row.result for row in tool_receipts(directory / "state")]
    terminal = next((row for row in reversed(events) if row.get("type") == "task_done"), None)
    answer = str(terminal.get("summary") or "") if terminal is not None else ""
    cited_body = cited_page_text(receipts, case)
    trace = directory / "handoff.jsonl"
    phases: list[JsonObject] = ([decode_json_object(json.loads(line))
                                for line in trace.read_text(encoding="utf-8").splitlines()] if trace.exists() else [])
    early = [row["turn"] for row in phases if row["phase"] == "submit"
             and not all(contains_fact(json.dumps(row["evidence"], ensure_ascii=False), fact) for fact in case.facts)]
    stable = before == production_source_digest()
    row: JsonObject = {"arm": arm, "stop_reason": result.stop_reason, "answer": answer, "source_stable": stable,
        "passed": stable and result.stop_reason == "done" and all(
            contains_fact(answer, fact) and contains_fact(cited_body, fact) for fact in case.facts),
        "early_submit_only_turns": early, "assessment_calls": sum(row.call.args.get("code") == PROGRAM for row in receipts),
        "browser_reads": sum(row.ok and row.call.name == "browser_read" for row in receipts),
        "missing_checks": sum(row["status"] == "missing" and row["phase"] == "collect" for row in phases),
        "waits": sum(row.ok and row.call.name == "browser_wait" for row in receipts), **provider_metrics(directory / "provider.jsonl")}
    write_json(directory / "summary.json", row)
    return row


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-dir", required=True, type=Path)
    parser.add_argument("--base-url", default="http://127.0.0.1:5001/v1")
    parser.add_argument("--max-turns", type=int, default=10)
    parser.add_argument("--separate-url", action="store_true", help="Put the URL on a separate line.")
    parser.add_argument("--candidate-only", action="store_true", help="Recheck a revised candidate against the recorded baseline.")
    args = parser.parse_args()
    target = select_target(90, args.base_url)
    args.run_dir.mkdir(parents=True, exist_ok=False)
    matrix: tuple[tuple[Arm, Arm], ...] = (("A", "B"), ("B", "A"), ("A", "B"))
    runs: list[JsonObject] = []
    for (name, case), order in zip(CASES.items(), matrix, strict=True):
        for arm in order:
            if args.candidate_only and arm == "A":
                continue
            print(f"Starting {name}/{arm}", flush=True)
            row = run_arm(args.run_dir / f"{name}-{arm}", case, arm, target, max_turns=args.max_turns, separate_url=args.separate_url)
            runs.append({"case": name, **row})
            write_json(args.run_dir / "summary.json", {"model": target.model, "runs": runs})
            print({key: row[key] for key in ("passed", "stop_reason", "model_calls", "assessment_calls", "early_submit_only_turns")}, flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
