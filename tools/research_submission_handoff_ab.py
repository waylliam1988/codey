"""A/B experiment for switching explicit browser Research from acquisition to submission."""
from __future__ import annotations

import argparse
import json
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass, replace
from pathlib import Path
from unittest.mock import patch

from codey.app.headless_runner import HeadlessRequest, HeadlessResult, run_headless
from codey.codemode.catalog import build_codemode_catalog, render_declarations
from codey.operations.browser_completion import complete_page_passages, evaluate_research_sequence
from codey.operations.kernel_context import KernelContext
from codey.operations.kernel_protocol import TurnSnapshot
from codey.policies.browser_requirement import BrowserResearchSequence
from codey.policies.tool_requirement import parse_tool_success_check
from codey.providers.base import ProviderToolResult
from codey.providers.text_result_views import TextResultSpan
from codey.research.answer_contract import AnswerScope
from codey.runtime.core.execution_record import ExecutionRecord
from codey.runtime.core.models import ToolResult
from codey.runtime.core.operation_payload import RuntimeExecutionCounters, RuntimeOperationState
from codey.toolchain.tool_spec import json_contract_text
from codey.types.json_value import JsonObject, decode_json_object
from tools import browser_production_live_gate as gate
from tools.browser_outcome_ab import DELAYED_HTML
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

RULE = BrowserResearchSequence(gate.URL, "text", "鱼丸")


@dataclass(frozen=True)
class Handoff:
    phase: str
    reason: str
    labels: tuple[str, ...] = ()


def handoff(rule: BrowserResearchSequence, snapshot: TurnSnapshot, *, records: Mapping[str, ExecutionRecord],
            results: Mapping[str, ToolResult], document: JsonObject | None) -> Handoff:
    scope, policy = snapshot.research_scope, snapshot.policy
    if not isinstance(scope, AnswerScope) or policy is None or rule.declaration not in policy.required_checks:
        return Handoff("collect", "out_of_scope")
    outcome = evaluate_research_sequence(rule, records=records, results=results, document=document, report=None)
    missing = {requirement.tool for check in policy.required_checks
               if (requirement := parse_tool_success_check(check)) is not None
               and not requirement.observe(records, results=results).evaluation.passed}
    if outcome.reason_code == "browser_read_after_wait_missing" and missing <= {"browser_read"}:
        return Handoff("read", outcome.reason_code)
    if outcome.reason_code != "browser_post_wait_citation_missing" or missing:
        return Handoff("collect", outcome.reason_code if not missing else "required_tools_missing")
    passages = {passage for ref in outcome.evidence_refs if (result := results.get(ref)) is not None
                and result.call.name == "browser_read" for passage in complete_page_passages(result, rule.url)}
    labels = tuple(label for label, ref in scope.aliases if ref in passages)
    return Handoff("submit", outcome.reason_code, labels) if labels else Handoff("read", "citation_unavailable")


def narrow(snapshot: TurnSnapshot, step: Handoff) -> TurnSnapshot:
    nested: tuple[str, ...]
    if step.phase == "submit" and "submit_research_answer" in snapshot.tool_names:
        names, nested = ("submit_research_answer",), ()
    elif step.phase == "read" and "codemode" in snapshot.tool_names and "browser_read" in snapshot.nested_tool_names:
        names, nested = ("codemode",), ("browser_read",)
    else:
        return snapshot
    inventory = (*names, *nested)
    specs = tuple(spec for spec in snapshot.frozen_specs if spec.name in inventory)
    if nested:
        declarations = render_declarations(build_codemode_catalog(specs, allow_writes=False))
        specs = tuple(replace(spec, declarations=declarations) if spec.name == "codemode" else spec for spec in specs)
    return replace(snapshot, tool_names=names, nested_tool_names=nested, frozen_specs=specs,
        contract_text=json_contract_text(tuple(spec for spec in specs if spec.name in names)),
        custom_executors=tuple(row for row in snapshot.custom_executors if row[0] in inventory))


@contextmanager
def candidate(directory: Path, arm: Arm, *, rule: BrowserResearchSequence = RULE) -> Iterator[None]:
    original = KernelContext.freeze

    def freeze(context: KernelContext, state: RuntimeOperationState, snapshot: TurnSnapshot, *, turn: int,
               prompt: str = "", messages: list[ProviderToolResult] | None = None, acknowledge: bool = False,
               counters: RuntimeExecutionCounters | None = None, text_result_spans: tuple[TextResultSpan, ...] = (),
               coding_context_prepared: bool = False, initial_source_batch: bool = False) -> None:
        if isinstance(snapshot.research_scope, AnswerScope) and not acknowledge:
            tools = context.execution.research_tools
            browser = tools.browser_session if tools is not None else None
            step = handoff(rule, snapshot, records=context.session.executed, results=context.session._memory_results,
                           document=browser.document_identity() if browser is not None else None)
            if arm == "B":
                snapshot = narrow(snapshot, step)
                hint = ""
                if step.phase == "submit":
                    labels = ", ".join(f"[{label}]" for label in step.labels)
                    hint = f"The required post-wait page evidence is ready. Use submit_research_answer and cite {labels}."
                elif step.phase == "read":
                    hint = "The required wait succeeded. Read the complete current document with browser_read in CodeMode."
                if hint:
                    prompt += "\n\n" + hint
                    if messages:
                        messages[-1] = replace(messages[-1], user_content=(messages[-1].user_content or "") + "\n\n" + hint)
            append_json(directory / "handoff.jsonl", {"turn": turn, "phase": step.phase, "reason": step.reason,
                "labels": list(step.labels), "tools": list(snapshot.tool_names), "nested": list(snapshot.nested_tool_names)})
        original(context, state, snapshot, turn=turn, prompt=prompt, messages=messages, acknowledge=acknowledge,
            counters=counters, text_result_spans=text_result_spans, coding_context_prepared=coding_context_prepared,
            initial_source_batch=initial_source_batch)

    with patch.object(KernelContext, "freeze", freeze):
        yield


def run_arm(directory: Path, arm: Arm, target: Target, *, delayed: bool = False, max_turns: int = 10) -> JsonObject:
    before = production_source_digest()
    def run(request: HeadlessRequest, *, emit_jsonl: Callable[[dict[str, object]], None],
            connect_provider: Callable[[str], ExperimentProvider]) -> HeadlessResult:
        return run_headless(replace(request, required_tools=(*request.required_tools, RULE.declaration)),
                            emit_jsonl=emit_jsonl, connect_provider=connect_provider)

    with (patch.object(gate, "run_headless", run), candidate(directory, arm),
          patch("codey.app.provider_services.provider_tab_availability", return_value={}),
          patch("codey.app.review_service.run_review", return_value=None)):
        row = decode_json_object(gate.run_case(directory, "research", target, strict_research=True, max_turns=max_turns,
            fixture_html=DELAYED_HTML if delayed else gate.HTML))
    stable = before == production_source_digest()
    handoffs = directory / "handoff.jsonl"
    phases = [decode_json_object(json.loads(line)) for line in handoffs.read_text(encoding="utf-8").splitlines()] if handoffs.exists() else []
    ready = next((entry["turn"] for entry in phases if entry["phase"] == "submit"), None)
    receipts = tool_receipts(directory / "state") if (directory / "state" / "durable.sqlite3").exists() else []
    submissions = [r.result for r in receipts if r.result.call.name == "submit_research_answer"]
    events = directory / "events.jsonl"
    activity = [decode_json_object(json.loads(line)) for line in events.read_text(encoding="utf-8").splitlines()] if events.exists() else []
    row.update(arm=arm, delayed=delayed, source_stable=stable, source_digest=before, required_sequence=RULE.declaration,
               passed=stable and row.get("passed") is True, ready_turn=ready,
               reads_after_ready=sum(entry.get("type") == "tool" and entry.get("tool_name") == "browser_read"
                   and type(turn := entry.get("turn")) is int and type(ready) is int and turn >= ready for entry in activity),
               submit_calls=len(submissions), successful_submissions=sum(r.ok for r in submissions),
               **provider_metrics(directory / "provider.jsonl"))
    write_json(directory / "summary.json", row)
    return row


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-dir", type=Path, required=True)
    parser.add_argument("--base-url", default="http://127.0.0.1:5001/v1")
    parser.add_argument("--timeout", type=float, default=90)
    args = parser.parse_args()
    target = gate.select_target(args.timeout, args.base_url)
    args.run_dir.mkdir(parents=True, exist_ok=False)
    rows: list[JsonObject] = []
    matrix: tuple[tuple[bool, tuple[Arm, Arm]], ...] = ((False, ("A", "B")), (True, ("B", "A")))
    for delayed, order in matrix:
        for arm in order:
            label = "delayed" if delayed else "original"
            print(f"Starting {label}/{arm}", flush=True)
            row = run_arm(args.run_dir / f"{label}-{arm}", arm, target, delayed=delayed)
            rows.append(row)
            write_json(args.run_dir / "summary.json", {"model": target.model, "runs": rows})
            print({key: row.get(key) for key in ("passed", "stop_reason", "model_calls", "ready_turn", "source_reads", "submit_calls")}, flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
