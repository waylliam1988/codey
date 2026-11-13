"""Compare CodeMode with four additional direct leaves through the production kernel.

Only build_turn_snapshot is wrapped. No tool definition, executor, permission,
reply, automatic readback or completion decision is replaced.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import time
from collections import Counter
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, replace
from pathlib import Path
from unittest.mock import patch

from playwright.sync_api import Route

from codey.app.headless_runner import HeadlessRequest, run_headless
from codey.automation.browser_session import BrowserSession
from codey.env_names import NATIVE_TOOLS_ENV
from codey.operations import kernel_protocol
from codey.operations.kernel_protocol import KernelSessionView, TurnSnapshot, refresh_snapshot_contract
from codey.operations.provider_receipts import restore_snapshot, snapshot_description
from codey.policies.tool_requirement import _browser_body, decode_answer
from codey.research.browser_search import BrowserSearchProvider
from codey.runtime.core.models import ToolResult
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

DIRECT_TOOLS = ("browser_snapshot", "browser_fill", "browser_click", "browser_read")
URL = "https://example.com/leaf-exposure-record"


@dataclass(frozen=True)
class Case:
    question: str
    html: str
    facts: tuple[str, ...]
    required: tuple[str, ...]
    field_value: str = ""


def form_html(label: str, value: str, facts: str, *, multiline: bool = False) -> str:
    field = '<textarea id="value"></textarea>' if multiline else '<input id="value">'
    expected = json.dumps(value, ensure_ascii=False)
    result = json.dumps(facts, ensure_ascii=False)
    return (f'<label>{label}{field}</label><button onclick=\'window.clicks=(window.clicks||0)+1;'
            f'if(document.getElementById("value").value==={expected})'
            f'{{document.getElementById("record").textContent={result};}}\'>查询记录</button>'
            '<article id="record">尚未查询，请先填写信息，再查询记录。</article>')


NOTE = '用户说"明天到"\n请保留原文'
CASES = {
    "visible": Case("请报告 LY-72 的可借数量和今天闭馆时间，并注明本次页面证据的引用。",
        '<article>馆藏 LY-72：可借 6 本。今天 19:40 闭馆。</article>', ("6", "19:40"), ()),
    "click": Case("请查询 B-19 的实时状态和库位，读取查询后的页面正文，并注明证据引用。",
        '<section aria-label="批次 B-19"><h2>批次 B-19</h2><article id="record">实时记录尚未查询。</article>'
        '<button onclick="window.clicks=(window.clicks||0)+1;document.getElementById(\'record\').textContent='
        "'批次 B-19：已入库，库位 C-42。'\">查询实时记录</button></section>",
        ("已入库", "C-42"), ("browser_click", "browser_read")),
    "form": Case("请在城市输入框填写福州，查询记录。读取查询后的正文，报告净重和校验码，并注明证据引用。",
        form_html("城市", "福州", "福州样本：净重 173 克，校验码 QUARTZ_851。"),
        ("173", "QUARTZ_851"), ("browser_fill", "browser_click", "browser_read"), "福州"),
    "quoted": Case('请在备注框填写以下两行，保留双引号和换行：\n用户说"明天到"\n请保留原文\n'
        "然后查询记录。读取查询后的正文，报告受理状态和回执码，并注明证据引用。",
        form_html("备注", NOTE, "备注已受理，回执码 NOTE_624。", multiline=True),
        ("已受理", "NOTE_624"), ("browser_fill", "browser_click", "browser_read"), NOTE),
}
PARAPHRASES = {
    "visible": "LY-72 现在能借几本？今天几点关门？回答时请标注页面证据。",
    "click": "帮我查一下 B-19 入库了吗、现在放在哪里。请完成页面上的查询，读回结果，再附上引用。",
    "form": "把城市设成福州后查一查，这个样本有多少克，校验码是什么？请读取操作后的记录并注明来源。",
    "quoted": '备注请原样输入两行：\n用户说"明天到"\n请保留原文\n录入后查询，读回受理结果和回执码，并附上来源引用。',
}


def expose_leaves(snapshot: TurnSnapshot, session: KernelSessionView) -> TurnSnapshot:
    """Add reachability only; definitions, nested capabilities and grants stay frozen."""
    if (session.task_kind != "chat" or snapshot.input_binding is not None or snapshot.browser_copy is not None
            or "codemode" not in snapshot.tool_names):
        return snapshot
    specs = {spec.name: spec for spec in snapshot.frozen_specs}
    exposure = session.model_tool_names
    selected = tuple(name for name in DIRECT_TOOLS
        if name in snapshot.nested_tool_names and name in specs
        and snapshot.policy is not None and snapshot.policy.allows(specs[name].grant)
        and (snapshot.allowed is None or name in snapshot.allowed)
        and name not in session.controller_denied and (exposure is None or name in exposure))
    names = tuple(dict.fromkeys((*snapshot.tool_names, *selected)))
    inventory = tuple(dict.fromkeys((*names, *snapshot.nested_tool_names)))
    executors = dict(snapshot.custom_executors)
    return refresh_snapshot_contract(replace(snapshot, tool_names=names,
        frozen_specs=tuple(specs[name] for name in inventory),
        custom_executors=tuple((name, executors[name]) for name in inventory)))


@contextmanager
def experiment(directory: Path, arm: Arm) -> Iterator[None]:
    original = kernel_protocol.build_turn_snapshot

    def build(session: KernelSessionView, *, native: bool = False) -> TurnSnapshot:
        baseline = original(session, native=native)
        candidate = expose_leaves(baseline, session) if arm == "B" else baseline
        payload = snapshot_description(candidate)
        restored = restore_snapshot(payload)
        if restored != candidate:
            raise AssertionError("snapshot roundtrip changed the experiment contract")
        if (candidate.policy != baseline.policy or candidate.allowed != baseline.allowed
                or candidate.nested_tool_names != baseline.nested_tool_names
                or {s.name: s for s in candidate.frozen_specs} != {s.name: s for s in baseline.frozen_specs}
                or dict(candidate.custom_executors) != dict(baseline.custom_executors)):
            raise AssertionError("exposure changed execution authority or definitions")
        append_json(directory / "snapshots.jsonl", {"baseline_tools": list(baseline.tool_names),
            "added": [name for name in candidate.tool_names if name not in baseline.tool_names], "snapshot": payload})
        return restored

    with patch.object(kernel_protocol, "build_turn_snapshot", build):
        yield


def response_actions(path: Path) -> list[JsonObject]:
    actions: list[JsonObject] = []
    if not path.exists():
        return actions
    for line in path.read_text(encoding="utf-8").splitlines():
        row = decode_json_object(json.loads(line))
        if row.get("type") != "response":
            continue
        payload = decode_json_object(row["payload"])
        choices = payload.get("choices")
        if not isinstance(choices, list) or not choices:
            continue
        message = decode_json_object(decode_json_object(choices[0])["message"])
        content = message.get("content")
        if isinstance(content, str):
            try:
                actions.append(decode_json_object(decode_answer(content)))
            except (ValueError, TypeError):
                actions.append({"invalid_reply": content})
    return actions


def metrics(receipts: list[ToolResult], actions: list[JsonObject]) -> JsonObject:
    attempts = Counter(str(action.get("tool")) for action in actions)
    calls = Counter(row.call.name for row in receipts if row.call.name.startswith("browser_"))
    successes = Counter(row.call.name for row in receipts if row.ok and row.call.name.startswith("browser_"))
    direct = Counter(row.call.name for row in receipts if row.call.name in DIRECT_TOOLS
                     and ":child:" not in row.call.call_id)
    errors = [row for row in receipts if not row.ok]
    contract_errors = [row for row in errors if row.audit.get("error_code") == "result_contract"
        or any(text in row.model_text for text in ("unwrap expected", "nonexistent property", "Undeclared result field"))]
    return {"direct_attempts": {name: attempts[name] for name in DIRECT_TOOLS},
        "direct_executions": dict(direct), "browser_calls": dict(calls), "browser_successes": dict(successes),
        "javascript_proposals": sum(action.get("tool") == "codemode" and isinstance(action.get("args"), dict)
            and isinstance(decode_json_object(action["args"]).get("code"), str) for action in actions),
        "invalid_replies": sum("invalid_reply" in action for action in actions),
        "result_contract_errors": len(contract_errors),
        "stale_ref_errors": sum(isinstance(row.canonical.get("value"), dict)
            and decode_json_object(row.canonical["value"]).get("reason") == "stale_ref" for row in errors),
        "syntax_errors": sum(row.audit.get("error_code") == "syntax_error" for row in errors),
        "failed_receipts": len(errors)}


def evidence_checks(receipts: list[ToolResult], answer: str, facts: tuple[str, ...]) -> JsonObject:
    """Resolve answer citations against real receipt passages, never against all page text."""
    citations = set(re.findall(r"\[([^\[\]\s]+)\]", answer))
    matching = []
    cited = []
    exact = []
    for row in receipts:
        if not row.ok or row.call.name not in {"browser_read", "open_url"}:
            continue
        body = _browser_body(row)
        if body is None or not all(fact in body for fact in facts):
            continue
        matching.append(row.call.name)
        # Passage references are the canonical identifiers advertised by Chat.
        passages = row.canonical.get("read_passages")
        refs = {str(passage.get("ref")) for passage in passages if isinstance(passage, dict)} \
            if isinstance(passages, list) else set()
        if citations.intersection(refs):
            exact.append(row.call.name)
            cited.append(row.call.name)
        elif row.canonical.get("source_id") in citations:
            cited.append(row.call.name)
    return {"facts_in_read_receipts": bool(matching), "updated_browser_read": "browser_read" in matching,
        "citations": sorted(citations), "correct_evidence_cited": bool(cited),
        "updated_source_cited": "browser_read" in cited,
        "exact_passage_cited": bool(exact)}


def run_arm(directory: Path, case: Case, arm: Arm, target: Target, *, max_turns: int = 10) -> JsonObject:
    before = production_source_digest()
    script_digest = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
    directory.mkdir(parents=True, exist_ok=False)
    browser = BrowserSearchProvider(headless=True)
    provider = ExperimentProvider(target, directory, fixed_acquisition=False)
    events: list[JsonObject] = []
    page_state: JsonObject = {}
    document: JsonObject | None = None
    initial_document: JsonObject | None = None
    bound_before_model = False
    started = time.monotonic()
    task = f"请打开 {URL}。{case.question}"
    write_json(directory / "input.json", {"task": task, "model": target.model, "seed": target.seed,
        "temperature": 0, "max_turns": max_turns, "required_tools": list(case.required),
        "capabilities": ["web.read", "browser.observe", "browser.interact"], "initial_source_required": True})

    def route(request: Route) -> None:
        request.fulfill(status=200 if request.request.url == URL else 404, content_type="text/html",
            body='<html><head><meta charset="utf-8"><title>查询页面</title></head><body>' + case.html + '</body></html>'
            if request.request.url == URL else "Fixture unavailable")

    def observe(event: dict[str, object]) -> None:
        nonlocal page_state, document, initial_document, bound_before_model
        value = decode_json_object(event)
        events.append(value)
        append_json(directory / "events.jsonl", value)
        if value.get("type") == "tool":
            provider.observed_tools += 1
            # Passive verifier only, before production task cleanup closes its page.
            session = BrowserSession.from_provider(browser)
            document = session.document_identity()
            if document is not None:
                if initial_document is None:
                    initial_document = document
                if value.get("tool_name") == "open_url" and value.get("turn") == 0:
                    bound_before_model = True
                state = session.evaluate('() => ({clicks:window.clicks||0,value:document.getElementById("value")?.value||""})')
                if state.ok:
                    page_state = decode_json_object(state.observation.get("value", {}))

    def connect(_provider_id: str, **_kwargs: object) -> ExperimentProvider:
        return provider

    try:
        with (experiment(directory, arm), patch.dict(os.environ, {NATIVE_TOOLS_ENV: "1"}),
              patch.object(browser, "_guard_request", route),
              patch("codey.research.search_factory.default_research_search_provider", return_value=browser),
              patch("codey.operations.task_phases.dispatch.default_research_search_provider", return_value=browser),
              patch("codey.app.provider_services.provider_availability", return_value={"local": True}),
              patch("codey.app.provider_services.provider_tab_availability", return_value={}),
              patch("codey.app.review_service.run_review", return_value=None)):
            result = run_headless(HeadlessRequest(None, task, intent="chat", provider_id="local", max_turns=max_turns,
                state_home=directory / "state", research_store_root=directory / "knowledge",
                requested_capabilities=("web.read", "browser.observe", "browser.interact"),
                sources_open_required=True, required_tools=case.required,
                model_selection={"model": target.model, "base_url": target.base_url}),
                emit_jsonl=observe, connect_provider=connect)
    finally:
        browser.close()
        provider.close()
    receipts = [item.result for item in tool_receipts(directory / "state")]
    actions = response_actions(directory / "provider.jsonl")
    terminal = next((row for row in reversed(events) if row.get("type") == "task_done"), None)
    answer = str(terminal.get("summary") or "") if terminal is not None else ""
    evidence = evidence_checks(receipts, answer, case.facts)
    facts_match = all(fact in answer for fact in case.facts)
    successful = {row.call.name for row in receipts if row.ok}
    stable = before == production_source_digest()
    row: JsonObject = {"arm": arm, "stop_reason": result.stop_reason, "answer": answer,
        "source_stable": stable, "source_digest": before, "script_digest": script_digest,
        "seconds": round(time.monotonic() - started, 3), "bound_before_model": bound_before_model,
        "facts_match": facts_match, "evidence": evidence, "document": document, "initial_document": initial_document,
        "page_state": page_state,
        "passed": stable and bound_before_model and result.stop_reason == "done"
            and facts_match and bool(evidence["correct_evidence_cited"])
            and set(case.required) <= successful and (not case.required or bool(evidence["updated_browser_read"]))
            and (not case.field_value or page_state.get("value") == case.field_value),
        **metrics(receipts, actions), **provider_metrics(directory / "provider.jsonl")}
    write_json(directory / "actions.json", actions)
    write_json(directory / "summary.json", row)
    return row


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-dir", required=True, type=Path)
    parser.add_argument("--base-url", default="http://127.0.0.1:5001/v1")
    parser.add_argument("--max-turns", type=int, default=10)
    parser.add_argument("--repeats", type=int, default=1)
    parser.add_argument("--wordings", nargs="+", choices=("original", "paraphrase"), default=["original"])
    parser.add_argument("--cases", nargs="+", choices=tuple(CASES), default=list(CASES))
    args = parser.parse_args()
    if args.repeats < 1 or args.max_turns < 1:
        parser.error("repeats and max-turns must be positive")
    target = select_target(90, args.base_url)
    args.run_dir.mkdir(parents=True, exist_ok=False)
    runs: list[JsonObject] = []
    for wording_index, wording in enumerate(args.wordings):
        for repeat in range(args.repeats):
            for index, name in enumerate(args.cases):
                case = replace(CASES[name], question=PARAPHRASES[name]) if wording == "paraphrase" else CASES[name]
                order: tuple[Arm, Arm] = ("A", "B") if (index + repeat + wording_index) % 2 == 0 else ("B", "A")
                for arm in order:
                    print(f"Starting {wording}/{repeat + 1}/{name}/{arm}", flush=True)
                    row = run_arm(args.run_dir / f"{wording}-r{repeat + 1}-{name}-{arm}", case, arm, target,
                                  max_turns=args.max_turns)
                    runs.append({"case": name, "wording": wording, "repeat": repeat + 1, **row})
                    write_json(args.run_dir / "summary.json", {"model": target.model, "seed": target.seed, "runs": runs})
                    print({key: row[key] for key in ("passed", "model_calls", "javascript_proposals", "direct_executions")}, flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
