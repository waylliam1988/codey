"""Compare ordered browser evidence against the unchanged production completion gate."""
from __future__ import annotations

import argparse
import json
from collections.abc import Callable, Generator, Iterator, Mapping
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass, replace
from pathlib import Path
from typing import ParamSpec
from unittest.mock import patch

from codey.completion.contract import CHECK_FAIL, CHECK_PASS, CompletionCheck
from codey.operations import completion_gate
from codey.operations.completion_inputs import CompletionCheckInput
from codey.operations.task_execution import ExecutionDelegate
from codey.operations.task_session import TaskSession
from codey.policies.tool_requirement import ToolObservation, _browser_body
from codey.research.answer_contract import AnswerScope
from codey.research.ledger_receipts import source_observation
from codey.runtime.core.models import ToolCall, ToolResult
from codey.types.json_value import JsonObject, decode_json_object, decode_json_value
from tools import browser_production_live_gate as gate
from tools import protocol_surface_ab as probe
from tools.completion_artifact_audit import EffectReceipt, tool_receipts
from tools.completion_explanation_ab import (
    Arm,
    ExperimentProvider,
    Target,
    append_json,
    production_source_digest,
    provider_metrics,
    write_json,
)
from tools.protocol_surface import script_body
from tools.receipt_field_assembly_ab import wait_for_idle

CHECK = "ordered_browser_prerequisites"
DOCUMENT = "experiment_browser_document"
WAIT = "unwrap(await tools.browser_wait({condition:'text',value:'鱼丸',timeout_ms:5000}));"
NAVIGATE = "unwrap(await tools.browser_navigate({url:inputs.url}));"
LOAD = ("let s=unwrap(await tools.browser_snapshot({}));"
        "unwrap(await tools.browser_fill({ref:s.elements.find(e=>e.name==='Locality').ref,value:'福州'}));"
        "s=unwrap(await tools.browser_snapshot({}));"
        "unwrap(await tools.browser_click({ref:s.elements.find(e=>e.name==='Load record').ref}));")
READ = "unwrap(await tools.browser_read({}))"
ROUND = NAVIGATE + LOAD + WAIT + f"return {READ};"
SUMMARY = "两轮加载完成，净重 173 克，校验码 QUARTZ_851。"
CASES = ("missing_wait", "wrong_wait", "old_read", "other_tab", "old_citation", "page_reset")
LIVE_CASES = ("old_read", "old_citation", "page_reset")
P = ParamSpec("P")


def code_action(code: str) -> JsonObject:
    return {"tool": "codemode", "args": {"code": code, "inputs": {"url": gate.URL}}}


def submission(label: str, *, current: bool) -> JsonObject:
    facts = "净重 173 克，校验码 QUARTZ_851" if current else "净重 101 克，校验码 OLD_101"
    return {"tool": "submit_research_answer", "args": {"state": "answered", "markdown": f"福州样本{facts}。 [{label}]"}}


def prefix(case: str) -> list[JsonObject]:
    if case == "page_reset":
        return [code_action(ROUND), {"tool": "done", "args": {"summary": SUMMARY}}]
    code = NAVIGATE + f"const old={READ};" + LOAD
    if case == "other_tab":
        code += ("const original=unwrap(await tools.browser_tab_list({})).tabs.find(t=>t.url===inputs.url).index;"
                 "unwrap(await tools.browser_tab_new({url:inputs.url}));{" + LOAD + WAIT + "}"
                 "unwrap(await tools.browser_tab_switch({index:original}));" + f"const latest={READ};")
    elif case == "wrong_wait":
        code += WAIT.replace("鱼丸", "福州")
    elif case != "missing_wait":
        code += WAIT
    if case == "old_citation":
        code += f"const latest={READ};"
    return [code_action(code + "return old;"), submission("e1", current=False)]


@dataclass(frozen=True)
class Progress:
    next_step: str = ""
    feedback: str = ""
    evidence: tuple[str, ...] = ()
    rounds: int = 0

    def payload(self) -> JsonObject:
        return {"next_step": self.next_step, "feedback": self.feedback, "evidence": [ref for ref in self.evidence],
                "rounds": self.rounds}


def expected_wait(result: ToolResult) -> bool:
    value = result.canonical.get("value")
    return (result.ok and result.call.args.get("condition") == "text" and result.call.args.get("value") == "鱼丸"
            and isinstance(value, dict) and value.get("matched") is True and value.get("page_url") == gate.URL)


def browser_document(result: ToolResult) -> JsonObject | None:
    document = result.audit.get(DOCUMENT)
    return decode_json_object(document) if isinstance(document, dict) else None


def passage_refs(result: ToolResult) -> set[str]:
    passages = result.canonical.get("read_passages")
    if not isinstance(passages, list):
        return set()
    try:
        source, _, _ = source_observation(result.canonical.get("research_observation"))
        if source.final_url != gate.URL or not _browser_body(result):
            return set()
    except (KeyError, TypeError, ValueError):
        return set()
    return {ref for row in passages if isinstance(row, dict) and isinstance(ref := row.get("ref"), str)}


def research_progress(receipts: list[EffectReceipt]) -> Progress:
    current = next((browser_document(r.result) for r in reversed(receipts)
                    if r.result.call.name in probe.PAGE_TRANSITIONS
                    or (r.result.ok and r.result.call.name in {"browser_wait", "browser_read"})), None)
    if current is None or current.get("url") != gate.URL:
        return Progress("browser_navigate", f"先导航到指定研究页面 {gate.URL}。")
    wait_ref = ""
    reads: dict[str, tuple[str, str]] = {}
    for receipt in receipts:
        result = receipt.result
        if not result.ok or browser_document(result) != current:
            continue
        if result.call.name == "browser_wait" and expected_wait(result):
            wait_ref = receipt.identity
        if wait_ref and result.call.name == "browser_read":
            reads.update((ref, (wait_ref, receipt.identity)) for ref in passage_refs(result))
    if not wait_ref:
        return Progress("browser_wait", "当前研究页面缺少成功的 browser_wait。请先等待 condition=text、value=鱼丸。")
    if not reads:
        return Progress("browser_read", "当前研究页面已完成指定等待。请在同一页面执行 browser_read，再引用新正文。", (wait_ref,))
    last = next((r for r in reversed(receipts) if r.result.ok and r.result.call.name == "submit_research_answer"), None)
    submitted = last.result.canonical.get("research_submission") if last is not None else None
    citations = submitted.get("citations") if isinstance(submitted, dict) else None
    refs = {ref for row in citations if isinstance(row, dict) and isinstance(ref := row.get("passage_ref"), str)} \
        if isinstance(citations, list) else set()
    if not refs or not refs <= reads.keys():
        witnesses = tuple(dict.fromkeys(ref for pair in reads.values() for ref in pair))
        scope = AnswerScope.from_payload(submitted["scope"]) if isinstance(submitted, dict) else None
        labels = "、".join(f"[{label}]" for label, ref in scope.aliases if ref in reads) if scope is not None else ""
        return Progress("submit_research_answer", "等待后的正文已经读取。请重提 submit_research_answer，引用 "
                        + (labels or "该次正文的 [eN]") + "。", witnesses)
    assert last is not None
    witnesses = tuple(dict.fromkeys(ref for passage in sorted(refs) for ref in reads[passage]))
    return Progress(evidence=(*witnesses, last.identity))


def reset_progress(receipts: list[EffectReceipt]) -> Progress:
    # The existing matcher verifies the action sequence and snapshot refs. Here
    # wait and read must also identify the very document created by that round.
    candidates: list[EffectReceipt] = []
    document: JsonObject | None = None
    for receipt in receipts:
        result = receipt.result
        name = result.call.name
        if name in probe.PAGE_TRANSITIONS:
            document = browser_document(result) if probe.fixture_navigation_value(result) is not None else None
        if name in {"browser_wait", "browser_read"} and (
                document is None or browser_document(result) != document
                or (name == "browser_wait" and not expected_wait(result))):
            continue
        candidates.append(receipt)
    complete, partial = probe.page_reset_progress(candidates)
    evidence = tuple(ref for row in complete for ref in row) + tuple(partial)
    if len(complete) == 2:
        return Progress(evidence=evidence, rounds=2)
    step = probe.PAGE_RESET_STEPS[len(partial)]
    return Progress(step, f"页面加载已完成 {len(complete)}/2 轮。第 {len(complete) + 1} 轮下一步是 {step}。",
                    evidence, len(complete))


@contextmanager
def observe_completion(directory: Path, case: str, arm: Arm) -> Generator[None]:
    original = completion_gate.evaluate
    progress = Progress()
    profile = "chat" if case == "page_reset" else "research"
    check_progress = reset_progress if case == "page_reset" else research_progress

    def check(_input: CompletionCheckInput) -> list[CompletionCheck]:
        return [CompletionCheck(CHECK, CHECK_FAIL if progress.next_step else CHECK_PASS, progress.next_step)]

    def evaluate(session: TaskSession, done_text: object, *, context: object = None,
                 observations: Mapping[str, ToolObservation] | None = None) -> completion_gate.GateVerdict:
        nonlocal progress
        progress = check_progress(tool_receipts(directory / "state"))
        verdict = original(session, done_text, context=context, observations=observations)
        append_json(directory / "prerequisite-checks.jsonl", {**progress.payload(), "accepted": verdict.complete})
        if arm == "B" and progress.next_step and not verdict.complete and not verdict.blocked:
            return replace(verdict, followup=progress.feedback + "\n\n" + verdict.followup)
        return verdict

    if arm == "B":
        completion_gate.register_completion_check_provider(CHECK, check, profile=profile)
    try:
        with patch.object(completion_gate, "evaluate", evaluate):
            yield
    finally:
        if arm == "B":
            completion_gate.unregister_completion_check_provider(CHECK)


def with_prefix(generate: Callable[P, dict[str, object]], provider: ExperimentProvider,
                actions: Iterator[JsonObject]) -> Callable[P, dict[str, object]]:
    def seeded(*args: P.args, **kwargs: P.kwargs) -> dict[str, object]:
        action = next(actions, None)
        if action is None:
            return generate(*args, **kwargs)
        provider.exchange += 1
        append_json(provider.directory / "provider.jsonl", {"type": "scripted_prefix", "exchange": provider.exchange, "action": action})
        return script_body(action)
    return seeded


def run_arm(directory: Path, case: str, arm: Arm, target: Target, *, max_turns: int = 7) -> JsonObject:
    base_browser = ExecutionDelegate._execute_browser
    actions = iter(prefix(case))

    def provider_factory(selected: Target, path: Path, **_kwargs: object) -> ExperimentProvider:
        provider = ExperimentProvider(selected, path, fixed_acquisition=False)
        provider_patches.enter_context(patch.object(provider, "_generate", with_prefix(provider._generate, provider, actions)))
        return provider

    def browser(delegate: ExecutionDelegate, call: ToolCall, *, turn: int, tool_index: int) -> tuple[ToolResult, bool, int | None]:
        session = delegate.browser_session
        reading = call.name in {"browser_wait", "browser_read"}
        before = session.document_identity() if session is not None and reading else None
        result, ok, code = base_browser(delegate, call, turn=turn, tool_index=tool_index)
        if session is not None and (call.name in probe.PAGE_TRANSITIONS or (reading and ok)):
            document = session.document_identity()
            if document is not None and (not reading or document == before):
                result = replace(result, audit={**result.audit, DOCUMENT: document})
        return result, ok, code

    def task(text: str) -> str:
        if case == "page_reset":
            return probe.task_text(text, "page_reset")
        return probe.task_text(text, "research") + ("研究要求：只用指定页面，在该页面等待文本鱼丸出现。"
            "等待成功后再在同一页面读取正文，最终只引用等待后正文的实际 [eN]。初始正文是旧缓存。")

    html = gate.HTML if case == "page_reset" else gate.HTML.replace("尚未加载记录。请填写 Locality 并点击 Load record。",
        "旧缓存：福州样本净重 101 克，校验码 OLD_101。请填写 Locality 并点击 Load record。")
    with (ExitStack() as provider_patches, patch.object(gate, "ExperimentProvider", provider_factory), patch.object(gate, "HTML", html),
          patch.object(ExecutionDelegate, "_execute_browser", browser), observe_completion(directory, case, arm),
          patch("codey.app.review_service.run_review", return_value=None),
          patch("codey.app.provider_services.provider_tab_availability", return_value={})):
        row = decode_json_object(gate.run_case(directory, "chat" if case == "page_reset" else "research", target,
                               strict_research=case != "page_reset", max_turns=max_turns, task_transform=task))
    checks = directory / "prerequisite-checks.jsonl"
    lines: list[str] = checks.read_text(encoding="utf-8").splitlines() if checks.exists() else []
    decisions = [decode_json_object(json.loads(line)) for line in lines]
    terminal: JsonObject = decisions[-1] if decisions else {}
    row.update(case=case, arm=arm, checks=decode_json_value(decisions), early_accepted=any(
        item.get("accepted") is True and bool(item.get("next_step")) for item in decisions),
        passed=row["stop_reason"] == "done" and row["facts_match"] is True and terminal.get("next_step") == "",
        **provider_metrics(directory / "provider.jsonl"))
    write_json(directory / "summary.json", row)
    return row


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-dir", type=Path, required=True)
    parser.add_argument("--case", choices=CASES, action="append")
    parser.add_argument("--timeout", type=float, default=90)
    parser.add_argument("--max-turns", type=int, default=7)
    parser.add_argument("--base-url", default="http://127.0.0.1:5001/v1")
    args = parser.parse_args()
    target = gate.select_target(args.timeout, args.base_url)
    root: Path = args.run_dir
    root.mkdir(parents=True)
    source = production_source_digest()
    cases = tuple(args.case or LIVE_CASES)
    write_json(root / "manifest.json", {"cases": cases, "model": target.model, "seed": target.seed,
                                       "production_digest": source, "max_turns": args.max_turns})
    rows: list[JsonObject] = []
    for index, case in enumerate(cases, 1):
        pair = root / f"{index:02d}-{case}"
        pair.mkdir()
        order: tuple[Arm, Arm] = ("A", "B") if index % 2 else ("B", "A")
        for arm in order:
            wait_for_idle(target.base_url)
            print(f"{case} {arm}", flush=True)
            rows.append(run_arm(pair / arm, case, arm, target, max_turns=args.max_turns))
            write_json(root / "progress.json", rows)
    unchanged = source == production_source_digest()
    write_json(root / "summary.json", {"production_unchanged": unchanged, "runs": rows})
    return 0 if unchanged else 1


if __name__ == "__main__":
    raise SystemExit(main())
