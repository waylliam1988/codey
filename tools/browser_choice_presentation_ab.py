"""Presentation-only A/B over existing production browser choices and executors.

A receives current production requests. B appends cards constructed only from
the current frozen observation and production handoff_call. No reply, tool
definition, authorization, executor, completion gate or snapshot is changed.
Recovery prefixes are explicitly scripted and counted separately from natural
tasks. Controlled Chromium fixtures measure transfer across page structures,
not reliability of arbitrary public websites.
"""
from __future__ import annotations

import argparse
import hashlib
import html
import json
import re
import subprocess
import time
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from unittest.mock import patch

from codey.operations.browser_task_handoff import handoff_call
from codey.operations.chat_citations import render_source_citations
from codey.operations.kernel_context import KernelContext
from codey.operations.kernel_protocol import TurnSnapshot
from codey.operations.task_session import TaskSession
from codey.policies.tool_requirement import decode_answer
from codey.providers.base import ProviderToolDefinition
from codey.research.ledger import ResearchLedger
from codey.research.passages import restore_passages
from codey.runtime.core.models import ToolCall, ToolResult
from codey.types.json_value import JsonObject, canonical_json_object, decode_json_object, parse_json_object
from codey.utils.citation_scanner import mask_markdown_code
from tools import browser_production_live_gate as gate
from tools.completion_artifact_audit import tool_receipts
from tools.completion_explanation_ab import (
    Arm,
    ExperimentProvider,
    Target,
    append_json,
    production_source_digest,
    provider_metrics,
    service_state,
    write_json,
)

ROOT = Path(__file__).resolve().parents[1]
CASES = ("early_break", "partial", "reordered", "duplicate", "slow", "pagination", "visible", "file_copy")
PREFIX = 'function counted(){window.counts.clicks++;}window.counts={clicks:0,fills:0,wrong:0};'
CARD_HEADER = "Existing production action choices (presentation experiment; no additional capabilities):"


def render(value: JsonObject) -> str:
    return json.dumps(value, ensure_ascii=False)


@dataclass(frozen=True)
class Case:
    name: str
    body: str
    task: str
    amount: int
    code: str
    clicks: int
    fills: int
    prefixes: tuple[JsonObject, ...] = ()
    mode: str = "chat"


def fixture(name: str, repeat: int) -> Case:
    """Expected answers belong only to the page fixture and passive verifier."""
    amount = 173 if name in {"early_break", "partial"} else 211 + repeat * 37 + CASES.index(name)
    code = "QUARTZ_851" if name in {"early_break", "partial"} else f"REC_{repeat + 1}_{CASES.index(name)}_Q7"
    result = f"鱼丸净重为 {amount} 克。记录校验码 {code}。"
    result_js = json.dumps(result, ensure_ascii=False)
    standard = ('<label>Locality<input id="value" oninput="window.counts.fills++"></label>'
        '<button aria-controls="record" onclick="counted();if(document.getElementById(\'value\').value===\'福州\')'
        f'{{document.getElementById(\'record\').textContent={html.escape(result_js, quote=True)};}}">Load record</button>'
        '<article id="record">尚未加载记录。</article>')
    prefixes: tuple[JsonObject, ...] = ()
    question = "把 Locality 填为福州，再查询记录，报告实际净重和校验码。"
    body, clicks, fills, mode = standard, 1, 1, "chat"
    if name == "early_break":
        saved = parse_json_object((ROOT / "tests/fixtures/browser_choice_early_break_20261119.json").read_text(encoding="utf-8"))
        action: JsonObject = {"tool": "codemode", "args": {"code": saved["code"], "inputs": {}, "language": "javascript"}}
        prefixes = (action, action)
    elif name == "partial":
        saved = parse_json_object((ROOT / "tests/fixtures/browser_choice_early_break_20261119.json").read_text(encoding="utf-8"))
        prefixes = ({"tool": "codemode", "args": {"code": saved["partial_code"], "inputs": {}}},)
    elif name == "reordered":
        body = standard.replace("Locality", "Destination").replace("Load record", "Check availability")
        split = body.index("<button")
        end = body.index("</button>") + len("</button>")
        body = body[split:end] + body[:split] + body[end:]
        question = "查一下福州的 Destination 记录：净重和校验码分别是什么？"
    elif name == "duplicate":
        # Both sections have identical field/button names; only context distinguishes them.
        target = standard.replace("Locality", "Record number").replace("Load record", "Search").replace("福州", "NB-41")
        other = ('<section aria-label="历史记录"><h2>历史记录</h2><label>Record number<input></label>'
                 '<button onclick="counted();window.counts.wrong++">Search</button>'
                 '<article>只包含旧批次 NB-40。</article></section>')
        body = other + '<section aria-label="当前库存"><h2>当前库存</h2>' + target + '</section>'
        question = "在当前库存中查 NB-41 的净重和校验码，历史记录不用查。"
    elif name == "slow":
        body = standard.replace("Locality", "城市").replace("Load record", "查询最新记录")
        # Filling replaces the button node; result text appears 3.2 seconds after click.
        body = body.replace('oninput="window.counts.fills++"', 'oninput="window.counts.fills++;const b=document.querySelector(\'button\');b.replaceWith(b.cloneNode(true))"')
        assignment = f"document.getElementById('record').textContent={html.escape(result_js, quote=True)};"
        body = body.replace(assignment, "document.getElementById('record').textContent='正在查询，请稍候';"
                            f"setTimeout(()=>{{{assignment}}},3200);")
        question = "查福州的最新净重和校验码，以查询完成后的记录为准。"
    elif name == "pagination":
        detail = f'<button aria-controls="record" onclick="counted();document.getElementById(\'record\').textContent={html.escape(result_js, quote=True)}">打开 ZX-62</button>'
        # json.dumps is embedded into a single-quoted attribute through HTML escaping.
        detail_js = html.escape(json.dumps(detail, ensure_ascii=False), quote=True)
        body = ('<p>当前第 1 页，仅有 ZX-61。ZX-62 在下一页。</p><div id="list">'
                f'<button onclick="counted();document.getElementById(\'list\').innerHTML={detail_js}">下一页</button>'
                '</div><article id="record">尚未打开记录。</article>')
        question, clicks, fills = "找到 ZX-62 的记录，告诉我它的净重和校验码。", 2, 0
    elif name == "visible":
        body = f'<h1>记录 ZX-62</h1><article>{result}</article>' + (
            '<button onclick="counted();window.counts.wrong++">打印</button>'
            '<button onclick="counted();window.counts.wrong++">删除记录</button>')
        question, clicks, fills = "ZX-62 的净重和校验码是什么？只需查看。", 0, 0
    elif name == "file_copy":
        body = standard.replace("Locality", "城市").replace("Load record", "获取测量")
        question = ("查福州的净重和校验码。把实测净重填入项目 result.json 的 amount，"
                    "校验码填入 code，保留其他字段，并报告结果。")
        mode = "hybrid"
    elif name not in CASES:
        raise ValueError("Unknown case")
    if name in {"slow", "file_copy"}:
        # The initial open_url path checks readable source content. A tiny
        # Chinese-only form otherwise fails before the model sees any choices.
        body = '<h1>库存记录查询服务</h1><p>这里显示当前查询记录，请以查询后的详情为准。</p>' + body
    task = (f"请打开 {gate.URL}。{question}读取当前有效正文，回答注明本次实际来源引用。"
            "Use codemode only. 不要运行命令、测试或修改无关内容。")
    document = ('<!doctype html><html><head><meta charset="utf-8"><title>Record portal</title></head>'
                f'<body><script>{PREFIX}</script>{body}</body></html>')
    return Case(name, document, task, amount, code, clicks, fills, prefixes, mode)


def cards(session: TaskSession, snapshot: TurnSnapshot) -> str:
    """No fixture names, expected answers, business target or next step oracle."""
    if snapshot.input_binding is not None:
        bound: dict[str, object] = {"tool": snapshot.input_binding.model_spec.name,
                                  "parameters": dict[str, object](snapshot.input_binding.model_spec.parameters)}
        return (CARD_HEADER + "\nThe current production input is already bound. Use its offered parameters directly; "
                "do not write another JavaScript program or copy values manually.\n"
                + render(canonical_json_object(bound)))
    lines = [CARD_HEADER,
        "Choose the intended target and values yourself using the task and current observations. "
        "Use the existing one-step call instead of writing a search loop. A CodeMode wrapper below is fixed; "
        "supply missing inputs as data. Reuse verified completed actions. A click alone does not prove the answer; "
        "read current result evidence. The choices include irrelevant controls; do not operate them unless needed."]
    for name in ("browser_snapshot", "browser_read"):
        if name in snapshot.nested_tool_names:
            _, action, error = handoff_call(session, snapshot, ToolCall(name, {}))
            if action is not None:
                lines.append(render({"operation": name, "call": action}))
            elif error:
                lines.append(error)
    references = snapshot.browser_references
    if references is not None:
        seen: set[str] = set()
        for choice in references.choices():
            ref = references.resolve(choice)
            if ref in seen or not isinstance(choice, dict):
                continue
            seen.add(ref)
            name = "browser_fill" if choice["role"] == "textbox" else "browser_click"
            if name not in snapshot.nested_tool_names:
                continue
            for purpose in ((None,) if name == "browser_fill" else (None, "result", "controls", "action")):
                args: JsonObject = {"ref": choice}
                if purpose is not None:
                    args["purpose"] = purpose
                _, action, error = handoff_call(session, snapshot, ToolCall(name, args))
                if action is not None or name == "browser_fill" and "supply value" in error:
                    lines.append(render({"target": choice, "operation": name, "purpose": purpose,
                                         "call": action, "missing_input_guidance": error}))
    return "\n".join(lines)


class ChoiceProvider(ExperimentProvider):
    def __init__(self, target: Target, directory: Path, arm: Arm, prefixes: tuple[JsonObject, ...]) -> None:
        super().__init__(target, directory, fixed_acquisition=False)
        self.arm, self.prefixes = arm, prefixes
        self.presentation = ""

    def configure_experiment(self, payload: dict[str, object]) -> None:
        super().configure_experiment(payload)
        if self.arm == "B" and self.presentation:
            detached = canonical_json_object(payload)
            messages = detached["messages"]
            assert isinstance(messages, list)
            payload["messages"] = [*messages, {"role": "user", "content": self.presentation}]

    def _generate(self, messages: list[dict[str, object]], tools: list[ProviderToolDefinition] | None = None,
                  *, timeout: float | None = None, checkpoints: list[dict[str, object]] | None = None,
                  tool_call_json: bool = False, tool_call_schema: dict[str, object] | None = None,
                  tool_call_contract: str = "", tool_call_observations: str = "",
                  tool_call_history_groups: int = 0, output_token_limit: int = 0,
                  require_tool_call: bool = False) -> dict[str, object]:
        if self.exchange < len(self.prefixes):
            action = self.prefixes[self.exchange]
            self.exchange += 1
            body: dict[str, object] = {"choices": [{"message": {"role": "assistant", "content": json.dumps(action),
                "tool_calls": []}, "finish_reason": "stop"}]}
            append_json(self.directory / "provider.jsonl", {"type": "scripted_failure_prefix",
                        "exchange": self.exchange, "payload": body})
            return body
        return super()._generate(messages, tools, timeout=timeout, checkpoints=checkpoints,
            tool_call_json=tool_call_json, tool_call_schema=tool_call_schema, tool_call_contract=tool_call_contract,
            tool_call_observations=tool_call_observations, tool_call_history_groups=tool_call_history_groups,
            output_token_limit=output_token_limit, require_tool_call=require_tool_call)


def run_arm(directory: Path, case: Case, arm: Arm, target: Target, *, max_turns: int = 10) -> JsonObject:
    before = production_source_digest()
    recording: ChoiceProvider | None = None
    original = KernelContext.request_snapshot
    state: JsonObject = {}

    def provider_factory(selected: Target, root: Path, *, fixed_acquisition: bool = False,
                         acquisition_action: JsonObject | None = None, stream: bool = False) -> ChoiceProvider:
        nonlocal recording
        assert not fixed_acquisition and acquisition_action is None and not stream
        recording = ChoiceProvider(selected, root, arm, case.prefixes)
        return recording

    def capture(context: KernelContext, snapshot: TurnSnapshot, *,
                delivered_results: Sequence[ToolResult] = ()) -> tuple[TurnSnapshot, str]:
        nonlocal state
        result = original(context, snapshot, delivered_results=delivered_results)
        assert recording is not None
        recording.presentation = cards(context.session, result[0]) if arm == "B" else ""
        resources = context.execution.research_tools
        browser = resources.browser_session if resources is not None else None
        if browser is not None:
            generation = browser.generation

            state = decode_json_object(browser.inspect_state("window.counts || {}"))
            assert generation == browser.generation, "Passive verifier changed control generations"
        append_json(directory / "presentation.jsonl", {"arm": arm, "turn": context.session.turn,
            "text": recording.presentation, "actual_tools": list(result[0].tool_names),
            "delivered": [{"tool": row.call.name, "ok": row.ok} for row in delivered_results]})
        return result

    def task_transform(_original: str) -> str:
        return case.task

    # Reuse the fixture runner, production headless entry, browser and receipt verifier.
    with (patch.object(gate, "ExperimentProvider", provider_factory),
          patch.object(KernelContext, "request_snapshot", capture)):
        baseline = gate.run_case(directory, case.mode, target, task_transform=task_transform,
            fixture_html=case.body, expected_amount=case.amount, expected_code=case.code,
            codemode_only=True, explicit_wait=False, declare_required_tools=False, max_turns=max_turns)
    receipts = tool_receipts(directory / "state")
    results = [row.result for row in receipts]
    counts = Counter(row.call.name for row in results)
    successful = Counter(row.call.name for row in results if row.ok)
    actions = response_actions(directory / "provider.jsonl")
    evidence = verify_evidence(results, str(baseline.get("answer", "")), (str(case.amount), case.code))
    clicks = successful["browser_click"]
    fills = successful["browser_fill"]
    file_correct = baseline.get("final_file_correct") is True if case.mode == "hybrid" else True
    no_replay = (clicks == case.clicks and fills == case.fills and state.get("clicks") == case.clicks
                 and state.get("fills") == case.fills and state.get("wrong") == 0)
    source_stable = before == production_source_digest()
    passed = successful_run(baseline, evidence, source_stable=source_stable,
                            file_correct=file_correct, no_replay=no_replay)
    row: dict[str, object] = {"case": case.name, "arm": arm, "passed": passed,
        "stop_reason": str(baseline.get("stop_reason", "error")), "answer": str(baseline.get("answer", "")),
        "error": str(baseline.get("error", "")), "source_stable": source_stable,
        "scripted_prefixes": len(case.prefixes), "category": "recovery" if case.prefixes else "natural",
        "tool_calls": dict(counts), "successful_calls": dict(successful), "no_repeated_actions": no_replay,
        "file_correct": file_correct, "evidence": evidence, "page_state": state,
        "javascript_proposals": sum(action.get("tool") == "codemode" for action in actions),
        "failed_receipts": sum(not result.ok for result in results),
        "seconds": baseline.get("seconds"), **provider_metrics(directory / "provider.jsonl")}
    write_json(directory / "ab-summary.json", row)
    write_json(directory / "actions.json", actions)
    return canonical_json_object(row)


def response_actions(path: Path) -> list[JsonObject]:
    actions: list[JsonObject] = []
    if not path.exists():
        return actions
    for line in path.read_text(encoding="utf-8").splitlines():
        row = parse_json_object(line)
        if row.get("type") != "response":
            continue
        choices = decode_json_object(row["payload"])["choices"]
        assert isinstance(choices, list)
        message = decode_json_object(decode_json_object(choices[0])["message"])
        content = message.get("content")
        if isinstance(content, str):
            try:
                actions.append(decode_json_object(decode_answer(content)))
            except (ValueError, TypeError):
                actions.append({"invalid_reply": content})
    return actions


def verify_evidence(results: list[ToolResult], answer: str, facts: tuple[str, ...]) -> JsonObject:
    # Rebuild actual source identities and shown text; never infer evidence
    # from model JS return values, expected fixture values or source URLs alone.
    ledger = ResearchLedger()
    sources: dict[str, str] = {}
    browser_text: list[tuple[str, str]] = []
    for result in results:
        if not result.ok:
            continue
        canonical = canonical_json_object(result.canonical)
        if result.call.name in {"browser_read", "open_url"}:
            restore_passages(ledger, canonical.get("research_observation"), canonical.get("read_passages"))
            source_id = canonical.get("source_id")
            if isinstance(source_id, str):
                # A successful read must carry a validated passage binding.
                observation = decode_json_object(canonical["research_observation"])
                source = decode_json_object(observation["source"])
                url = source["final_url"]
                assert isinstance(url, str)
                assert source_id not in sources or sources[source_id] == url
                sources[source_id] = url
        elif result.call.name in {"browser_snapshot", "browser_click"}:
            value = decode_json_object(canonical["value"])
            url, text = value.get("page_url"), value.get("text")
            if isinstance(url, str) and isinstance(text, str):
                browser_text.append((url, text))
    prose = mask_markdown_code(answer)
    cited_urls = {url for identity, url in sources.items()
        if render_source_citations(f"[{identity}]", sources=sources, searches={}) in prose
        or re.search(re.escape(f"[{identity}]") + r"(?![\[(])", prose)}
    passages = [passage.excerpt for ref, passage in ledger.read_passages.items()
                if f"[{ref}]" in prose or passage.source.final_url in cited_urls]
    observations = [text for url, text in browser_text if url in cited_urls]
    formal = any(all(fact in text for fact in facts) for text in passages)
    observed = any(all(fact in text for fact in facts) for text in observations)
    return {"correct_evidence_cited": formal or observed, "formal_passage_evidence": formal,
            "observed_body_evidence": observed, "cited_passages": len(passages),
            "cited_browser_observations": len(observations)}


def successful_run(baseline: JsonObject | dict[str, object], evidence: JsonObject, *,
                   source_stable: bool, file_correct: bool, no_replay: bool) -> bool:
    return (source_stable and baseline.get("stop_reason") == "done" and baseline.get("facts_match") is True
            and evidence["correct_evidence_cited"] is True and file_correct and no_replay)


def wait_for_idle(base_url: str) -> None:
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline:
        state = service_state(base_url)
        if state.get("idle") == 1 and state.get("queue") == 0:
            return
        time.sleep(0.5)
    raise RuntimeError("Shared local model service remained busy for 60 seconds")


def freeze(root: Path) -> None:
    names = subprocess.check_output(["git", "ls-files", "--cached", "--others", "--exclude-standard", "--", "codey"],
                                    cwd=ROOT, text=True, encoding="utf-8").splitlines()
    write_json(root / "source-freeze.json", {"source_digest": production_source_digest(),
        "head": subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip(),
        "files": {name: hashlib.sha256((ROOT / name).read_bytes()).hexdigest() for name in sorted(names)}})


class Options(argparse.Namespace):
    run_dir: Path
    base_url: str
    repeats: int
    max_turns: int
    cases: list[str]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-dir", type=Path, required=True)
    parser.add_argument("--base-url", default="http://127.0.0.1:5001/v1")
    parser.add_argument("--repeats", type=int, default=2)
    parser.add_argument("--max-turns", type=int, default=10)
    default_cases: list[str] = list(CASES)
    parser.add_argument("--cases", nargs="+", choices=CASES, default=default_cases)
    args = parser.parse_args(namespace=Options())
    if args.repeats < 1 or args.max_turns < 1:
        parser.error("repeats and max-turns must be positive")
    root = args.run_dir.resolve()
    root.mkdir(parents=True, exist_ok=False)
    target = gate.select_target(90, args.base_url)
    freeze(root)
    source = production_source_digest()
    write_json(root / "manifest.json", {"interface": "browser-choice-presentation-only-v1", "cases": args.cases,
        "repeats": args.repeats, "seed_base": target.seed, "model": target.model, "temperature": 0,
        "max_turns": args.max_turns, "source_digest": source,
        "script_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        "treatment": "B appends current production action choices; all original messages and schemas remain intact",
        "limits": "Controlled pages; presentation only, no new structured interface, no claim of open-web reliability."})
    rows: list[JsonObject] = []
    for repeat in range(args.repeats):
        selected = Target(target.base_url, target.model, target.seed + repeat * 1009, target.timeout)
        for index, name in enumerate(args.cases):
            case = fixture(name, repeat)
            order: tuple[Arm, Arm] = ("A", "B") if (repeat + index) % 2 == 0 else ("B", "A")
            for arm in order:
                if production_source_digest() != source:
                    raise RuntimeError("Production source changed during frozen experiment")
                wait_for_idle(target.base_url)
                path = root / f"r{repeat + 1}-{name}-{arm}"
                print(render({"starting": path.name}), flush=True)
                row = run_arm(path, case, arm, selected, max_turns=args.max_turns)
                rows.append({"repeat": repeat + 1, **row})
                write_json(root / "summary.json", {"runs": rows, "source_before": source,
                    "source_after": production_source_digest()})
                print(render({key: row[key] for key in ("case", "arm", "passed", "stop_reason", "model_calls")}), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
