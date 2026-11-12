"""Compare receipt-rendered answers and post-load reads through the production browser fixture."""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
from collections.abc import Iterator, Mapping
from contextlib import contextmanager, nullcontext
from pathlib import Path
from subprocess import TimeoutExpired
from time import monotonic
from unittest.mock import patch

from codey.automation.browser_session import BrowserSession
from codey.completion.contract import CHECK_FAIL, CHECK_PASS, CompletionCheck
from codey.operations import completion_gate, task_execution
from codey.operations.browser_completion import _snapshot_ref, committed_results
from codey.operations.completion_context import CompletionContext
from codey.operations.completion_output import tool_facts
from codey.operations.task_execution import ExecutionDelegate
from codey.operations.task_session import TaskSession
from codey.policies.browser_requirement import PAGE_TRANSITIONS, browser_receipt_tools
from codey.policies.requirement import RequirementEvaluation
from codey.policies.tool_requirement import ToolObservation, _browser_body
from codey.research.finding_report import ResearchReport
from codey.runtime.core import cancellation
from codey.runtime.core.models import ToolCall, ToolResult
from codey.types.json_value import JsonObject, JsonValue, decode_json_object
from tools import browser_production_live_gate as gate
from tools.browser_three_fixes_ab import RECORD
from tools.completion_artifact_audit import tool_receipts
from tools.completion_explanation_ab import (
    ROOT,
    Target,
    append_json,
    production_source_digest,
    provider_metrics,
    write_json,
)

CASES = ("legacy_structured", "stale_edit", "stale_edit_delayed")
CHECK = "post_load_read_before_edit"
OBSERVED = frozenset({"browser_snapshot", "browser_click", "browser_read", *PAGE_TRANSITIONS})
DELAYED_HTML = gate.HTML.replace(
    "window.__record={amount:173,code:'QUARTZ_851'};",
    "document.querySelector('article').textContent='正在加载记录。';clearTimeout(window.__loadTimer);"
    "window.__loadTimer=setTimeout(() => {window.__record={amount:173,code:'QUARTZ_851'};",
).replace("} else { document", "}, 1500); } else { document")


def post_load_read(session: TaskSession, context: CompletionContext) -> RequirementEvaluation:
    records, results = tool_facts(session, context)
    committed = {child for row in records.values() if row["name"] == "codemode" and row["ok"]
                 for child in row.get("child_effect_ids", ())}
    document = None
    control = click = ""
    evidence: tuple[str, ...] = ()
    for ref, result in committed_results(records, results, OBSERVED | {"edit"}, include_failed=frozenset({"edit"})):
        name = result.call.name
        if name == "edit":
            break
        identity = result.audit.get("browser_document")
        if name in PAGE_TRANSITIONS:
            document = identity if isinstance(identity, dict) and identity.get("url") == gate.URL else None
            control = click = ""
            evidence = ()
        elif document is not None and identity == document:
            if name == "browser_snapshot":
                control = _snapshot_ref(result, "Load record", gate.URL)
            elif name == "browser_click":
                click = ref if control and result.call.args.get("ref") == control else ""
                control = ""
                evidence = ()
            elif click and ref in committed:
                try:
                    body = _browser_body(result, url=gate.URL)
                except (TypeError, ValueError, KeyError):
                    body = None
                matches = RECORD.findall(body or "")
                evidence = (click, ref) if len(matches) == 1 else ()
    if evidence:
        return RequirementEvaluation(evidence_refs=evidence)
    return RequirementEvaluation("loaded_record_missing",
        "Read the complete record on the same document after clicking Load record, before editing result.json.")


@contextmanager
def readiness_candidate(directory: Path, *, enabled: bool) -> Iterator[None]:
    execute = ExecutionDelegate._execute_browser_operation
    checks = completion_gate._required_tool_checks
    pending: dict[BrowserSession, JsonObject] = {}

    def browser_operation(delegate: ExecutionDelegate, browser: BrowserSession, call: ToolCall, *,
                          turn: int, tool_index: int) -> tuple[ToolResult, bool, int | None]:
        name = call.name
        if name in PAGE_TRANSITIONS:
            pending.pop(browser, None)
        if name == "browser_read" and (document := pending.get(browser)) is not None:
            pending.pop(browser)
            if browser.document_identity() == document:
                started = monotonic()
                wait = browser.wait("text", "记录校验码", 3000)
                append_json(directory / "readiness.jsonl", {"matched": wait.ok, "seconds": monotonic() - started})
                if not wait.ok:
                    pending[browser] = document
                    return ToolResult(call=call, ok=False, model_text=wait.model_text), False, None
        result = execute(delegate, browser, call, turn=turn, tool_index=tool_index)
        if name == "browser_click" and result[1]:
            identity = browser.document_identity()
            if identity is not None and identity.get("url") == gate.URL:
                pending[browser] = identity
        return result

    def required_checks(session: TaskSession, final_text: str, context: CompletionContext,
                        observations: Mapping[str, ToolObservation] | None,
                        report: ResearchReport | None) -> completion_gate._RequiredChecks:
        baseline = checks(session, final_text, context, observations, report)
        outcome = post_load_read(session, context)
        append_json(directory / "evidence.jsonl", {"passed": outcome.passed, "refs": list(outcome.evidence_refs)})
        if not enabled:
            return baseline
        return completion_gate._RequiredChecks(
            [*baseline.rows, CompletionCheck(CHECK, CHECK_PASS if outcome.passed else CHECK_FAIL, outcome.reason_code)],
            {**baseline.evaluations, CHECK: outcome}, baseline.tool_bindings)

    with (patch.object(ExecutionDelegate, "_execute_browser_operation", browser_operation) if enabled else nullcontext(),
          patch.object(completion_gate, "_required_tool_checks", required_checks),
          patch.object(task_execution, "browser_receipt_tools", lambda declared: browser_receipt_tools(declared) | OBSERVED)):
        yield


def run_arm(directory: Path, case: str, arm: str, target: Target, *, max_turns: int) -> JsonObject:
    before = production_source_digest()
    stale = case != "legacy_structured"
    candidate = arm == "B"
    with (readiness_candidate(directory, enabled=candidate) if stale else nullcontext(),
          patch("codey.app.provider_services.provider_tab_availability", return_value={}),
          patch("codey.app.review_service.run_review", return_value=None)):
        row = decode_json_object(gate.run_case(directory, "hybrid" if stale else "chat", target,
            max_turns=max_turns, structured=not stale and not candidate, explanation_output=not stale and candidate,
            stale_edit=stale, preserve_external_field=stale, explicit_wait=not (stale and candidate),
            fixture_html=DELAYED_HTML if case == "stale_edit_delayed" else gate.HTML))
    if not stale and candidate and row.get("passed") is True:
        answer = decode_json_object(json.loads(str(row["answer"])))
        rendered = f"{answer['explanation']}\n\n页面地址：{answer['page_url']}\nToken：{answer['token']}\n"
        (directory / "rendered-answer.txt").write_text(rendered, encoding="utf-8")
        row["rendered_answer"] = rendered
        row["passed"] = gate.legacy_urls_match(rendered)
    if stale and (directory / "state" / "durable.sqlite3").exists():
        receipts = tool_receipts(directory / "state")
        row["explicit_waits"] = sum(r.result.ok and r.result.call.name == "browser_wait" for r in receipts)
        for file in ("readiness", "evidence"):
            path = directory / f"{file}.jsonl"
            row[file] = [decode_json_object(json.loads(line))
                         for line in path.read_text(encoding="utf-8").splitlines()] if path.exists() else []
        evidence = row["evidence"]
        row["passed"] = (row["passed"] is True and isinstance(evidence, list) and bool(evidence)
                         and isinstance(evidence[-1], dict) and evidence[-1].get("passed") is True)
    stable = before == production_source_digest()
    row.update(case=case, arm=arm, source_stable=stable, passed=stable and row.get("passed") is True,
               **provider_metrics(directory / "provider.jsonl"))
    write_json(directory / "summary.json", row)
    return row


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-dir", required=True, type=Path)
    parser.add_argument("--worker", choices=CASES)
    parser.add_argument("--arm", choices=("A", "B"), default="A")
    parser.add_argument("--model")
    parser.add_argument("--timeout", default=90.0, type=float)
    parser.add_argument("--budget", default=300.0, type=float)
    parser.add_argument("--max-turns", default=10, type=int)
    parser.add_argument("--base-url", default="http://127.0.0.1:5001/v1")
    args = parser.parse_args()
    target = gate.select_target(args.timeout, args.base_url)
    root = args.run_dir.resolve()
    if args.worker:
        if args.model != target.model:
            parser.error("worker model differs from preflight model")
        run_arm(root, args.worker, args.arm, target, max_turns=args.max_turns)
        return 0
    root.mkdir(parents=True)
    source = production_source_digest()
    scripts = [Path(__file__), Path(gate.__file__)]
    hashes: list[JsonValue] = [hashlib.sha256(path.read_bytes()).hexdigest() for path in scripts]
    manifest: JsonObject = {"model": target.model, "seed": target.seed, "temperature": 0, "source": source,
        "script_hashes": hashes, "max_turns": args.max_turns, "timeout": args.timeout, "budget": args.budget}
    write_json(root / "manifest.json", manifest)
    runs: list[JsonObject] = []
    # Two text pairs and one pair per readiness fixture, balanced across arm order.
    for index, (case, order) in enumerate(zip(
        ("legacy_structured", "stale_edit", "legacy_structured", "stale_edit_delayed"), ("AB", "BA", "BA", "AB"), strict=True,
    ), 1):
        for arm in order:
            directory = root / f"{index}-{case}-{arm}"
            command = [sys.executable, "-m", "tools.browser_outcome_ab", "--run-dir", str(directory),
                       "--worker", case, "--arm", arm, "--model", target.model, "--timeout", str(args.timeout),
                       "--base-url", args.base_url, "--max-turns", str(args.max_turns)]
            print(f"Starting {index} {case}/{arm}", flush=True)
            process, owner = cancellation.start_process(command, cwd=ROOT)
            try:
                result = cancellation.wait_process(process, owner, command, args.budget, capture_limit_bytes=100_000)
                log = result.stdout + result.stderr
                error = f"worker exit {result.returncode}" if result.returncode else ""
            except TimeoutExpired:
                log, error = "", "worker exceeded budget"
            directory.mkdir(exist_ok=True)
            (directory / "worker.log").write_text(log, encoding="utf-8")
            row: JsonObject = ({"case": case, "arm": arm, "passed": False, "error": error or "missing summary"}
                   if error or not (directory / "summary.json").exists()
                   else decode_json_object(json.loads((directory / "summary.json").read_text(encoding="utf-8"))))
            runs.append(row)
            print(f"{index} {case}/{arm}: passed={row['passed']} stop={row.get('stop_reason')} calls={row.get('model_calls')} error={row.get('error')}", flush=True)
            write_json(root / "progress.json", {"runs": runs})
    stable = source == production_source_digest() and all(row.get("source_stable") is True for row in runs)
    scripts_stable = hashes == [hashlib.sha256(path.read_bytes()).hexdigest() for path in scripts]
    write_json(root / "summary.json", {**manifest, "runs": runs, "source_stable": stable, "scripts_stable": scripts_stable})
    return 0 if stable and scripts_stable and all(row["passed"] is True for row in runs) else 1


if __name__ == "__main__":
    raise SystemExit(main())
