"""Isolated A/B for three observed failures in the production browser fixture."""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from codey.automation.browser_session import BrowserSession, BrowserToolOutput
from codey.operations import kernel_control, task_execution
from codey.operations.browser_completion import committed_results
from codey.operations.completion_context import CompletionContext
from codey.operations.completion_output import CompletionOutput, prepare_completion_output, tool_facts
from codey.operations.task_session import TaskSession
from codey.policies.browser_requirement import PAGE_TRANSITIONS, browser_receipt_tools
from codey.policies.tool_requirement import _browser_body
from codey.types.json_value import JsonObject, JsonValue, decode_json_object
from tools import browser_production_live_gate as gate
from tools import receipt_bound_edit_ab as edit
from tools.completion_explanation_ab import Target, append_json, provider_metrics
from tools.protocol_surface_ab import artifact_metrics

SCENARIOS = ("legacy_structured", "reread", "stale_edit")
PROPERTY_RETURN = re.compile(r"\s*return[ \t]+(window\.[A-Za-z_$][\w$]*)\s*;?\s*", re.ASCII)
RECORD = re.compile(r"净重为\s*(\d+)\s*克。记录校验码\s+([A-Z0-9_]+)。")
READ_TOOLS = frozenset({"browser_read", "browser_click", *PAGE_TRANSITIONS})


def property_expression(source: str) -> str:
    match = PROPERTY_RETURN.fullmatch(source)
    return match[1] if match else source


def record_from_reads(session: TaskSession, context: CompletionContext) -> JsonObject | None:
    records, results = tool_facts(session, context)
    parented = {child for row in records.values() if row["name"] == "codemode" and row["ok"]
                for child in row.get("child_effect_ids", ())}
    document = None
    initial = click = ""
    latest = None
    for ref, result in committed_results(records, results, READ_TOOLS):
        name = result.call.name
        identity = result.audit.get("browser_document")
        if name in PAGE_TRANSITIONS:
            document = identity if isinstance(identity, dict) and identity.get("url") == gate.URL else None
            initial = click = ""
            latest = None
        elif document is None or identity != document:
            document = latest = None
        else:
            if name == "browser_click":
                click = ref
                latest = None
                continue
            try:
                body = _browser_body(result) if ref in parented else None
            except (KeyError, ValueError, TypeError):
                body = None
            if not click:
                if body and "尚未加载记录" in body:
                    initial = ref
                continue
            matches = RECORD.findall(body or "")
            latest = ({"initial_ref": initial, "click_ref": click, "read_ref": ref,
                       "amount": int(matches[0][0]), "code": matches[0][1]}
                      if initial and len(matches) == 1 else None)
    return latest


@contextmanager
def candidate(directory: Path, scenario: str, *, enabled: bool) -> Iterator[None]:
    if scenario == "stale_edit":
        with edit.prototype_helper(directory, enabled=enabled):
            yield
    elif not enabled:
        yield
    elif scenario == "legacy_structured":
        evaluate = BrowserSession._evaluate_on_browser_thread

        def evaluate_property(browser: BrowserSession, source: str) -> BrowserToolOutput:
            expression = property_expression(source)
            result = evaluate(browser, expression)
            if expression != source:
                append_json(directory / "candidate.jsonl",
                            {"original": source, "executed": expression, "ok": result.ok})
            return result

        with patch.object(BrowserSession, "_evaluate_on_browser_thread", evaluate_property):
            yield
    elif scenario == "reread":
        def assemble(session: TaskSession, text: str, context: CompletionContext) -> CompletionOutput:
            output = prepare_completion_output(session, text, context)
            record = record_from_reads(session, context)
            if record is None:
                return output
            append_json(directory / "candidate.jsonl", {**record, "model_text": text})
            return replace(output, text=f"加载后的页面记录显示净重 {record['amount']} 克，校验码 {record['code']}。")

        with (patch.object(kernel_control, "prepare_completion_output", assemble),
              patch.object(task_execution, "browser_receipt_tools", lambda checks: browser_receipt_tools(checks) | READ_TOOLS)):
            yield
    else:
        raise ValueError(f"unknown scenario: {scenario}")


def run_arm(directory: Path, scenario: str, arm: str, target: Target, *, max_turns: int) -> JsonObject:
    before = edit.production_source_digest()
    stale_edit = scenario == "stale_edit"
    with (candidate(directory, scenario, enabled=arm == "B"),
          patch("codey.app.provider_services.provider_tab_availability", return_value={}),
          patch("codey.app.review_service.run_review", return_value=None)):
        row = decode_json_object(gate.run_case(directory, "hybrid" if stale_edit else "chat", target,
            max_turns=max_turns, structured=scenario == "legacy_structured", reread=scenario == "reread",
            stale_edit=stale_edit, preserve_external_field=stale_edit))
    stable = before == edit.production_source_digest()
    log = directory / "candidate.jsonl"
    actions: list[JsonValue] = [decode_json_object(json.loads(line)) for line in log.read_text(encoding="utf-8").splitlines()
               ] if log.exists() else []
    row.update(scenario=scenario, arm=arm, source=before, source_stable=stable,
               passed=stable and row.get("passed") is True, candidate_actions=actions,
               metrics=artifact_metrics(directory), **edit.helper_metrics(directory),
               **provider_metrics(directory / "provider.jsonl"))
    edit.write_json(directory / "summary.json", row)
    return row


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-dir", type=Path, required=True)
    parser.add_argument("--worker", choices=SCENARIOS)
    parser.add_argument("--arm", choices=("A", "B"), default="A")
    parser.add_argument("--expected-model")
    parser.add_argument("--max-turns", type=int, default=10)
    parser.add_argument("--timeout", type=float, default=90)
    parser.add_argument("--budget", type=float, default=300)
    parser.add_argument("--kobold-base-url", default="http://127.0.0.1:5001/v1")
    args = parser.parse_args()
    target = gate.select_target(args.timeout, args.kobold_base_url)
    root = args.run_dir.resolve()
    if args.worker:
        if args.expected_model != target.model:
            parser.error("worker model differs from the matrix model")
        run_arm(root, args.worker, args.arm, target, max_turns=args.max_turns)
        return 0
    root.mkdir(parents=True)
    source = edit.production_source_digest()
    script = Path(__file__)
    script_hash = hashlib.sha256(script.read_bytes()).hexdigest()
    manifest: JsonObject = {"model": target.model, "seed": target.seed, "temperature": 0, "source": source,
                           "script_sha256": script_hash, "timeout": args.timeout, "budget": args.budget,
                           "max_turns": args.max_turns}
    edit.write_json(root / "manifest.json", manifest)
    rows: list[JsonObject] = []
    task_matches: JsonObject = {}
    for index, scenario in enumerate(SCENARIOS):
        for arm in ("AB" if index % 2 == 0 else "BA"):
            directory = root / f"{index + 1}-{scenario}-{arm}"
            print(f"Starting {scenario}/{arm}", flush=True)
            row = run_worker(directory, scenario, arm, target, budget=args.budget, max_turns=args.max_turns)
            rows.append(row)
            print(f"{scenario}/{arm}: passed={row['passed']} stop={row.get('stop_reason')} "
                  f"calls={row.get('model_calls')} error={row.get('error')}", flush=True)
            edit.write_json(root / "progress.json", list(rows))
        paths = [root / f"{index + 1}-{scenario}-{arm}" / "task.txt" for arm in ("A", "B")]
        task_matches[scenario] = all(path.exists() for path in paths) and paths[0].read_bytes() == paths[1].read_bytes()
    stable = source == edit.production_source_digest() and all(row.get("source_stable") for row in rows)
    script_stable = script_hash == hashlib.sha256(script.read_bytes()).hexdigest()
    edit.write_json(root / "summary.json", {**manifest, "runs": list(rows), "source_stable": stable,
                                          "script_stable": script_stable, "task_matches": task_matches})
    return 0 if stable and script_stable and all(task_matches.values()) and all(row["passed"] for row in rows) else 1


def run_worker(directory: Path, scenario: str, arm: str, target: Target, *, budget: float, max_turns: int) -> JsonObject:
    from subprocess import TimeoutExpired

    from codey.runtime.core import cancellation

    command = [sys.executable, "-m", "tools.browser_three_fixes_ab", "--run-dir", str(directory),
               "--worker", scenario, "--arm", arm, "--expected-model", target.model, "--timeout", str(target.timeout),
               "--kobold-base-url", target.base_url, "--max-turns", str(max_turns)]
    process, owner = cancellation.start_process(command, cwd=edit.ROOT)
    error = ""
    output = ""
    try:
        finished = cancellation.wait_process(process, owner, command, budget, capture_limit_bytes=100_000)
        output = finished.stdout + finished.stderr
        if finished.returncode:
            error = f"worker exited {finished.returncode}"
    except TimeoutExpired:
        error = f"worker exceeded {budget}s"
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "worker.log").write_text(output + error, encoding="utf-8")
    summary = directory / "summary.json"
    if not error and summary.exists():
        return decode_json_object(json.loads(summary.read_text(encoding="utf-8")))
    return {"scenario": scenario, "arm": arm, "passed": False, "error": error or "missing worker summary"}


if __name__ == "__main__":
    raise SystemExit(main())
