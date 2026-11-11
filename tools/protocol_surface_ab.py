"""Serial isolated A/B probes. --plan is offline; --diagnostics uses scripted HTTP, not a live model."""
from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys
import traceback
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager, nullcontext
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from codey.completion.contract import CHECK_FAIL, CHECK_PASS, CompletionCheck
from codey.operations import completion_gate
from codey.operations.completion_inputs import CompletionCheckInput
from codey.operations.task_session import TaskSession
from codey.policies.tool_requirement import ToolObservation
from codey.runtime.core import cancellation
from codey.runtime.core.models import ToolResult
from codey.types.json_value import JsonObject, decode_json_object, decode_json_value
from tools import browser_production_live_gate as gate
from tools import protocol_surface as surface
from tools import receipt_bound_edit_ab as edit
from tools import receipt_bound_edit_surface_probe as previous
from tools.completion_artifact_audit import EffectReceipt, tool_receipts
from tools.completion_explanation_ab import Target, append_json, provider_metrics

PAGE_RESET_GATE = "page-reset-gate"
PAGE_RESET_NAVIGATION = "page-reset-navigation"
PAGE_RESET_CONFIGS = {PAGE_RESET_GATE, PAGE_RESET_NAVIGATION}
PAGE_RESET_CHECK = "page_reset_two_rounds"
PAGE_TRANSITIONS = {"browser_navigate", "browser_tab_switch", "browser_tab_new"}
PAGE_RESET_STEPS = (
    "browser_navigate", "browser_snapshot", "browser_fill", "browser_click", "browser_wait", "browser_read",
)
PAGE_RESET_NAVIGATION_CODE = (
    "const navigation = await tools.browser_navigate({url:inputs.url});"
    "return {runtime_action:'page_reset_second_navigation',first_round_read:inputs.first_round_read,navigation,"
    "next:navigation.ok && navigation.value.http_status === 200 && navigation.value.url === inputs.url "
    "? '第二轮已导航。请重新观察、填写、点击、等待并读取。' : '第二轮导航未成功。请根据实际工具结果判断。'};"
)

SCENARIOS = {
    "B1": ("basic", "snapshot_restore", "stale_ref"),
    "B2": ("upload_bound", "structured"),
    "B3": ("legacy_structured", "upload", "hybrid", "stale_edit"),
    "B4": ("page_reset", "return_failure", "download"),
    PAGE_RESET_GATE: ("page_reset",),
    PAGE_RESET_NAVIGATION: ("page_reset",),
    "combined": (*previous.SCENARIOS, "page_reset", "return_failure"),
}
DIAGNOSTIC_SCENARIOS = {"return_failure", "source_failure_fixed", "upload_bound"}


def fixture_navigation_value(result: ToolResult) -> JsonObject | None:
    value = result.canonical.get("value")
    if (result.call.name == "browser_navigate" and result.ok and result.call.args.get("url") == gate.URL
            and isinstance(value, dict) and value.get("url") == gate.URL and value.get("http_status") == 200):
        return decode_json_object(value)
    return None


def page_reset_progress(receipts: list[EffectReceipt]) -> tuple[list[list[str]], list[str]]:
    """Find two real loads of the controlled fixture, each within its own navigation."""
    completed: list[list[str]] = []
    current: list[str] = []
    targets: dict[str, str] = {}
    page_id: object = None
    for receipt in receipts:
        result = receipt.result
        name, args = result.call.name, result.call.args
        value = result.canonical.get("value")
        if name in PAGE_TRANSITIONS:
            current, targets, page_id = [], {}, None
            if (navigation := fixture_navigation_value(result)) is not None:
                current.append(receipt.identity)
                page_id = navigation.get("page_id")
            continue
        if not current or not result.ok:
            continue
        if name == "browser_snapshot":
            targets = {}
            if (isinstance(value, dict) and value.get("page_id") == page_id and value.get("page_url") == gate.URL
                    and isinstance(elements := value.get("elements"), list)):
                for label in ("Locality", "Load record"):
                    refs = [element.get("ref") for element in elements
                            if isinstance(element, dict) and element.get("name") == label]
                    if len(refs) == 1 and isinstance(refs[0], str):
                        targets[label] = refs[0]
        if name != PAGE_RESET_STEPS[len(current)]:
            continue
        if name == "browser_snapshot" and "Locality" not in targets:
            continue
        if name == "browser_fill" and (args.get("value") != "福州" or args.get("ref") != targets.get("Locality")
                                       or "Locality" not in targets):
            continue
        if name == "browser_click" and (args.get("ref") != targets.get("Load record") or "Load record" not in targets):
            continue
        if name == "browser_wait" and (not isinstance(value, dict) or value.get("page_url") != gate.URL
                                       or value.get("matched") is not True):
            continue
        if name == "browser_read":
            observation = result.canonical.get("research_observation")
            source = observation.get("source") if isinstance(observation, dict) else None
            if (result.truncated or not isinstance(observation, dict) or not isinstance(source, dict)
                    or source.get("truncated") is not False or source.get("final_url") != gate.URL
                    or "QUARTZ_851" not in str(observation.get("text", ""))):
                continue
        current.append(receipt.identity)
        if len(current) == len(PAGE_RESET_STEPS):
            completed.append(current)
            current = []
            if len(completed) == 2:
                break
    return completed, current


def receipts_after(receipts: list[EffectReceipt], reference: str) -> list[EffectReceipt]:
    index = next(i for i, receipt in enumerate(receipts) if receipt.identity == reference)
    return receipts[index + 1:]


def page_reset_scheduler() -> Callable[[list[EffectReceipt]], JsonObject | None]:
    """Schedule one declared reset when the first complete read has committed."""
    scheduled = False

    def schedule(receipts: list[EffectReceipt]) -> JsonObject | None:
        nonlocal scheduled
        if scheduled:
            return None
        completed, _current = page_reset_progress(receipts)
        if len(completed) != 1:
            return None
        read_ref = completed[0][-1]
        if any(receipt.result.call.name in PAGE_TRANSITIONS for receipt in receipts_after(receipts, read_ref)):
            return None
        scheduled = True
        return {"tool": "codemode", "args": {"code": PAGE_RESET_NAVIGATION_CODE,
            "inputs": {"url": gate.URL, "first_round_read": read_ref}}}

    return schedule


def page_reset_navigation_audit(directory: Path, receipts: list[EffectReceipt]) -> JsonObject:
    log = directory / "provider.jsonl"
    actions = [event["exchange"] for line in log.read_text(encoding="utf-8").splitlines()
               if (event := decode_json_object(json.loads(line))).get("type") == "runtime_action"] if log.exists() else []
    parents = [row for row in receipts if row.result.call.name == "codemode"
               and row.result.call.args.get("code") == PAGE_RESET_NAVIGATION_CODE
               and "repair_of" not in row.result.call.args]
    if not parents:
        return {"scheduled_exchanges": actions, "parent_ref": None, "navigation_ref": None, "success": False}
    parent = parents[0]
    children = parent.result.canonical.get("child_effect_ids")
    child = next((row for row in receipts if isinstance(children, list) and row.identity in children
                  and row.result.call.name == "browser_navigate"), None)
    inputs = decode_json_object(parent.result.call.args["inputs"])
    child_result = None
    if child is not None:
        child_result = (decode_json_value(child.result.canonical.get("value")) if child.result.ok
                        else child.result.model_text)
    return {"scheduled_exchanges": actions, "parent_ref": parent.identity, "parent_count": len(parents),
            "first_round_read": inputs["first_round_read"], "navigation_ref": child.identity if child else None,
            "success": child is not None and fixture_navigation_value(child.result) is not None, "result": child_result}


@contextmanager
def page_reset_completion(directory: Path) -> Iterator[None]:
    """Add a fixture check to the production proof in this experiment process."""
    evaluate = completion_gate.evaluate
    observed: list[tuple[list[list[str]], list[str]]] = []

    def check(_input: CompletionCheckInput) -> list[CompletionCheck]:
        completed, current = page_reset_progress(tool_receipts(directory / "state"))
        observed.append((completed, current))
        if len(completed) == 2:
            return [CompletionCheck(PAGE_RESET_CHECK, CHECK_PASS)]
        missing = PAGE_RESET_STEPS[len(current)]
        return [CompletionCheck(PAGE_RESET_CHECK, CHECK_FAIL, f"page_reset_round_{len(completed) + 1}_missing_{missing}")]

    def evaluate_with_feedback(session: TaskSession, done_text: object, *, context: object = None,
                               observations: Mapping[str, ToolObservation] | None = None) -> completion_gate.GateVerdict:
        observed.clear()
        verdict = evaluate(session, done_text, context=context, observations=observations)
        if not observed:
            return verdict
        completed, current = observed[-1]
        missing = PAGE_RESET_STEPS[len(current)] if len(completed) < 2 else None
        append_json(directory / "page-reset-checks.jsonl", {
            "completed_rounds": len(completed), "round_receipts": completed, "partial_receipts": current,
            "next_missing_tool": missing, "done_accepted": verdict.complete,
        })
        if missing is None or verdict.complete or verdict.blocked:
            return verdict
        return replace(verdict, followup=(
            f"页面加载已完成 {len(completed)}/2 轮。第 {len(completed) + 1} 轮下一步是 {missing}。"
            "从这一步继续执行，完成本轮剩余操作后再提交 done。\n\n" + verdict.followup))

    completion_gate.register_completion_check_provider(PAGE_RESET_CHECK, check, profile="chat")
    try:
        with patch.object(completion_gate, "evaluate", evaluate_with_feedback):
            yield
    finally:
        completion_gate.unregister_completion_check_provider(PAGE_RESET_CHECK)


def task_text(task: str, scenario: str) -> str:
    """Shared business task for both arms; remove only obsolete interface instructions."""
    task = task.replace(
        "通过 codemode 的 tools.browser_navigate、browser_snapshot、browser_fill、browser_click、browser_wait、browser_read 完成操作。", "")
    task = task.replace(
        "每一步检查工具结果，不要猜 ref；用 unwrap 获得结构化 snapshot.elements，每项有 ref/role/name。", "每一步检查实际工具结果。")
    task = task.replace(
        "browser_read 返回字符串，直接 return unwrap(await tools.browser_read({}))，不要再取 .text。", "")
    task = task.replace("另外用 browser_eval 读取 window.__contractToken，取得返回对象的 value；", "另外读取页面 window.__contractToken 属性；")
    if scenario == "page_reset":
        task += ("恢复验收：第一次加载并读取记录后，再导航到同一 URL 重置页面。"
                 "重新观察、填写 Locality、点击 Load record、等待并读取记录。"
                 "两次加载都必须真实执行；历史填表回执不能证明重置后的表单状态。")
    if scenario == "return_failure":
        task += ("诊断首步由脚本实际下载并读取页面，随后故意返回编码失败。"
                 "根据正式回执恢复结果交付，不要重新下载；不确定时明确说明。")
    return task


def artifact_metrics(directory: Path) -> JsonObject:
    receipts = tool_receipts(directory / "state") if (directory / "state/durable.sqlite3").exists() else []
    path = directory / "protocol-events.jsonl"
    events = [decode_json_object(json.loads(line)) for line in path.read_text(encoding="utf-8").splitlines()] if path.exists() else []
    counts: JsonObject = {}
    for tool in ("browser_upload", "browser_download", "browser_navigate", "browser_fill", "browser_click"):
        counts[tool] = sum(row.result.ok and row.result.call.name == tool for row in receipts)
    counts["failed_parents"] = sum(row.result.call.name == "codemode" and not row.result.ok for row in receipts)
    counts["failed_steps"] = sum(row.result.call.name == "codemode" and row.result.ok
        and isinstance(value := row.result.canonical.get("value"), dict) and bool(value.get("failed_step")) for row in receipts)
    counts["mechanical_repairs"] = sum(event["kind"] == "receipt_repair" for event in events)
    counts["compiled_plans"] = sum(event["kind"] == "compiled_plan" for event in events)
    counts["static_rejections"] = sum(event["kind"] == "invalid_plan" for event in events)
    # Ordered leaf receipts, not parent counts, establish both completed cycles.
    expected = ["browser_navigate", "browser_fill", "browser_click", "browser_wait", "browser_read"] * 2
    cursor = 0
    for row in receipts:
        if cursor < len(expected) and row.result.ok and row.result.call.name == expected[cursor]:
            if row.result.call.name == "browser_read":
                source = row.result.canonical.get("research_observation")
                if not isinstance(source, dict) or "QUARTZ_851" not in str(source.get("text")):
                    continue
            cursor += 1
    counts["page_reset_completed"] = cursor == len(expected)
    counts["repaired_parent"] = any(row.result.ok and row.result.canonical.get("repaired_from") for row in receipts)
    return counts


def run_arm(directory: Path, scenario: str, arm: str, config: str, target: Target, *, max_turns: int) -> JsonObject:
    if config not in SCENARIOS or arm not in {"A", "B"} or scenario not in SCENARIOS["combined"]:
        raise ValueError("unknown experiment configuration, scenario or arm")
    before = edit.production_source_digest()
    base_scenario = {"page_reset": "basic", "return_failure": "download"}.get(scenario, scenario)
    mode, options = previous.scenario_options(base_scenario)
    if config in PAGE_RESET_CONFIGS and scenario != "page_reset":
        raise ValueError(f"{config} only runs page_reset")
    gate_enabled = config == PAGE_RESET_NAVIGATION or (config == PAGE_RESET_GATE and arm == "B")
    scheduler = page_reset_scheduler() if config == PAGE_RESET_NAVIGATION and arm == "B" else None
    active = config if arm == "B" and config not in PAGE_RESET_CONFIGS else "A"
    with (surface.prototype(directory, scenario, active, runtime_action=scheduler),
          page_reset_completion(directory) if gate_enabled else nullcontext(),
          patch("codey.app.provider_services.provider_tab_availability", return_value={}),
          patch("codey.app.review_service.run_review", return_value=None)):
        row = decode_json_object(gate.run_case(directory, mode, target, max_turns=max_turns,
            task_transform=lambda task: task_text(task, scenario), **options))
    after = edit.production_source_digest()
    metrics = artifact_metrics(directory)
    passed = row.get("passed") is True
    if scenario == "page_reset":
        passed = passed and metrics["page_reset_completed"] is True
    if config in PAGE_RESET_CONFIGS:
        receipts = tool_receipts(directory / "state")
        completed, current = page_reset_progress(receipts)
        metrics["verified_rounds"] = len(completed)
        metrics["next_missing_tool"] = PAGE_RESET_STEPS[len(current)] if len(completed) < 2 else None
        passed = passed and len(completed) == 2
        if config == PAGE_RESET_NAVIGATION:
            metrics["second_round_started"] = bool(completed) and any(
                fixture_navigation_value(receipt.result) is not None for receipt in receipts_after(receipts, completed[0][-1]))
            row["runtime_navigation"] = page_reset_navigation_audit(directory, receipts)
        row["page_reset_gate_enabled"] = gate_enabled
        checks = directory / "page-reset-checks.jsonl"
        row["page_reset_gate_checks"] = (
            [decode_json_object(json.loads(line)) for line in checks.read_text(encoding="utf-8").splitlines()]
            if checks.exists() else [])
    if scenario == "return_failure":
        passed = passed and metrics["browser_download"] == 1 and metrics["repaired_parent"] is True
    uploads, downloads = int(str(metrics["browser_upload"])), int(str(metrics["browser_download"]))
    artifact = row.get("project_result")
    external_lost = (scenario == "stale_edit" and row.get("changed_after_read") is True
                     and isinstance(artifact, dict) and "external_revision" not in artifact)
    row.update(config=config, active_config=active, arm=arm, scenario=scenario, source_before=before, source_after=after,
        source_stable=before == after, passed=passed and before == after, metrics=metrics,
        silent_failure=row.get("stop_reason") == "done" and not passed, external_field_lost=external_lost,
        duplicate_effect=uploads > 1 or downloads > 1,
        acquisition="scripted-fault-then-model" if scenario == "return_failure" else
                    "scripted-open-url" if scenario == "source_failure_fixed" else "autonomous-model",
        diagnostic=scenario in DIAGNOSTIC_SCENARIOS, **provider_metrics(directory / "provider.jsonl"))
    edit.write_json(directory / "summary.json", row)
    return row


def harness_digests() -> JsonObject:
    names = ("tools/protocol_surface_ab.py", "tools/protocol_surface.py", "tools/protocol_surface_program.js",
             "tools/structured_surface.py", "tools/mechanical_surface.py", "tools/receipt_bound_edit_surface_probe.py",
             "tools/receipt_bound_edit_ab.py", "tools/receipt_bound_edit_recovery_probe.py",
             "tools/browser_production_live_gate.py", "tools/completion_explanation_ab.py",
             "tools/completion_artifact_audit.py", "tests/test_protocol_surface.py")
    return {name: hashlib.sha256((edit.ROOT / name).read_bytes()).hexdigest() for name in names}


def run_worker(directory: Path, scenario: str, arm: str, config: str, target: Target,
               *, budget: float, max_turns: int) -> JsonObject:
    command = [sys.executable, "-m", "tools.protocol_surface_ab", "--run-dir", str(directory), "--config", config,
        "--worker-case", scenario, "--worker-arm", arm, "--expected-model", target.model,
        "--kobold-base-url", target.base_url, "--timeout", str(target.timeout), "--seed", str(target.seed),
        "--max-turns", str(max_turns)]
    process, owner = cancellation.start_process(command, cwd=edit.ROOT)
    error, output = "", ""
    try:
        result = cancellation.wait_process(process, owner, command, budget, capture_limit_bytes=100_000)
        output = result.stdout + result.stderr
        if result.returncode:
            error = f"worker exited {result.returncode}"
    except subprocess.TimeoutExpired:
        error = f"worker exceeded {budget}s; owned process tree terminated"
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "worker.log").write_text(output + error, encoding="utf-8")
    if not error and (directory / "summary.json").exists():
        return decode_json_object(json.loads((directory / "summary.json").read_text(encoding="utf-8")))
    row: JsonObject = {"scenario": scenario, "arm": arm, "config": config, "passed": False,
                       "error": error or "missing worker summary"}
    edit.write_json(directory / "worker-failure.json", row)
    return row


def qualification(rows: list[JsonObject], pairs: list[JsonObject], config: str, repeats: int) -> bool:
    expected = {(repeat, scenario) for repeat in range(1, repeats + 1) for scenario in SCENARIOS[config]}
    expected_units = {(repeat, scenario, arm) for repeat, scenario in expected for arm in ("A", "B")}
    actual = {(row.get("repetition"), row.get("scenario"), row.get("arm")) for row in rows}
    return (repeats >= 2 and actual == expected_units and len(rows) == len(expected_units)
            and len(pairs) == len(expected) and all(pair.get("comparable") is True for pair in pairs)
            and all(row.get("passed") is True and not row.get("duplicate_effect") and not row.get("silent_failure")
                    and not row.get("external_field_lost") for row in rows if row.get("arm") == "B"))


def check_evidence(paths: list[Path], target: Target) -> list[str]:
    """Evidence gates combination eligibility, not a claim of statistical superiority."""
    seen: set[str] = set()
    accepted: list[str] = []
    for path in paths:
        report = decode_json_object(json.loads((path / "summary.json").read_text(encoding="utf-8")))
        config = str(report.get("config"))
        if config not in {"B1", "B2", "B3", "B4"} or config in seen:
            raise ValueError("combination needs exactly one complete independent report for each B1-B4")
        rows = report.get("runs")
        pairs = report.get("pairs")
        repeats = report.get("repeats")
        if (not isinstance(rows, list) or not isinstance(pairs, list) or type(repeats) is not int
                or not qualification([decode_json_object(row) for row in rows],
                                     [decode_json_object(pair) for pair in pairs], config, repeats)
                or report.get("source_before") != edit.production_source_digest()
                or report.get("source_after") != report.get("source_before")
                or report.get("harnesses") != harness_digests() or report.get("harnesses_stable") is not True
                or report.get("model") != target.model or report.get("seed") != target.seed
                or report.get("base_url") != target.base_url or report.get("request_timeout") != target.timeout
                or report.get("version") != surface.VERSION):
            raise ValueError(f"{path}: incomplete, failed, changed-code or mismatched-model evidence")
        seen.add(config)
        accepted.append(str(path.resolve()))
    if seen != {"B1", "B2", "B3", "B4"}:
        raise ValueError("combined requires --evidence for B1, B2, B3 and B4 (at least two complete repeats each)")
    return accepted


def diagnostics(root: Path) -> int:
    root.mkdir(parents=True, exist_ok=False)
    before, hashes = edit.production_source_digest(), harness_digests()
    command = [sys.executable, "-m", "pytest", "-q", "tests/test_protocol_surface.py", "--junitxml", str(root / "junit.xml")]
    # No local model endpoint is contacted; tests install scripted HTTP.
    result = subprocess.run(command, cwd=edit.ROOT, check=False)
    stable = before == edit.production_source_digest() and hashes == harness_digests()
    edit.write_json(root / "diagnostics.json", {"version": surface.VERSION, "exit_code": result.returncode,
        "passed": result.returncode == 0 and stable, "harnesses": hashes, "source_stable": stable,
        "scope": "Deterministic mechanisms and scripted HTTP + real Chromium, not autonomous model results"})
    return 0 if result.returncode == 0 and stable else 1


def matrix(args: argparse.Namespace, target: Target) -> int:
    evidence = check_evidence(args.evidence, target) if args.config == "combined" else []
    root = args.run_dir.resolve()
    root.mkdir(parents=True, exist_ok=False)
    before, hashes = edit.production_source_digest(), harness_digests()
    selected = tuple(args.scenario) if args.scenario else SCENARIOS[args.config]
    manifest: JsonObject = {"version": surface.VERSION, "config": args.config, "scenarios": list(selected),
        "repeats": args.repeats, "model": target.model, "base_url": target.base_url, "seed": target.seed, "temperature": 0,
        "source_before": before, "harnesses": hashes, "request_timeout": target.timeout,
        "worker_budget": args.budget, "max_turns": args.max_turns, "combination_evidence": list(evidence),
        "scope": "Controlled Chromium; text JSON schema provider; shared neutral task. Optional review unavailable.",
        "diagnostics": [str(name) for name in sorted(DIAGNOSTIC_SCENARIOS)], "automatic_failure_reruns": False}
    edit.write_json(root / "manifest.json", manifest)
    frozen = root / "frozen-harness"
    for name in hashes:
        destination = frozen / name
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes((edit.ROOT / name).read_bytes())
    rows: list[JsonObject] = []
    pairs: list[JsonObject] = []
    for repeat in range(args.repeats):
        for index, scenario in enumerate(selected):
            pair = root / f"{repeat + 1:02d}-{scenario}"
            pair.mkdir()
            pair_rows = []
            for arm in (("A", "B") if (repeat + index) % 2 == 0 else ("B", "A")):
                print(json.dumps({"starting": scenario, "config": args.config, "arm": arm, "repetition": repeat + 1}), flush=True)
                row = run_worker(pair / arm, scenario, arm, args.config, target, budget=args.budget, max_turns=args.max_turns)
                row["repetition"] = repeat + 1
                rows.append(row)
                pair_rows.append(row)
                edit.write_json(root / "progress.json", {"runs": list(rows), "pairs": list(pairs)})
                if row.get("stop_reason") == "provider_failure" or row.get("error"):
                    edit.write_json(root / "interruption.json", {"status": "interrupted",
                        "reason": "Worker/provider failed; do not queue another generation after an uncertain outcome",
                        "scenario": scenario, "arm": arm, "repetition": repeat + 1,
                        "runs": list(rows), "pairs": list(pairs),
                        "source_stable": before == edit.production_source_digest(),
                        "harnesses_stable": hashes == harness_digests()})
                    return 1
            pairs.append(previous.pair_audit(pair, pair_rows))
    after = edit.production_source_digest()
    stable = before == after and hashes == harness_digests()
    per_arm: JsonObject = {}
    for arm in ("A", "B"):
        values = [row for row in rows if row["arm"] == arm]
        per_arm[arm] = {"passed": sum(row["passed"] is True for row in values), "total": len(values),
            "regular_passed": sum(row["passed"] is True for row in values if row["scenario"] not in DIAGNOSTIC_SCENARIOS),
            "regular_total": sum(row["scenario"] not in DIAGNOSTIC_SCENARIOS for row in values),
            "silent_failures": sum(row.get("silent_failure") is True for row in values),
            "duplicate_effects": sum(row.get("duplicate_effect") is True for row in values),
            "external_fields_lost": sum(row.get("external_field_lost") is True for row in values),
            "complete_usage_units": sum(row.get("usage_complete") is True for row in values),
            "model_calls": sum(int(str(row["model_calls"])) for row in values)
                           if all(type(row.get("model_calls")) is int for row in values) else None,
            "prompt_tokens": sum(int(str(row["prompt_tokens"])) for row in values)
                             if all(type(row.get("prompt_tokens")) is int for row in values) else None,
            "completion_tokens": sum(int(str(row["completion_tokens"])) for row in values)
                                 if all(type(row.get("completion_tokens")) is int for row in values) else None}
    outcomes: JsonObject = {"A_only": 0, "B_only": 0, "both": 0, "neither": 0, "incomparable": 0}
    for pair in pairs:
        label = ("incomparable" if pair["comparable"] is not True else "both" if pair["A_passed"] and pair["B_passed"]
                 else "A_only" if pair["A_passed"] else "B_only" if pair["B_passed"] else "neither")
        outcomes[label] = int(str(outcomes[label])) + 1
    report = {**manifest, "source_after": after, "source_stable": before == after,
        "harnesses_stable": hashes == harness_digests(), "runs": list(rows), "pairs": list(pairs), "arms": per_arm,
        "paired_outcomes": outcomes,
        "combination_eligible": (args.config not in PAGE_RESET_CONFIGS and stable
                                 and qualification(rows, pairs, args.config, args.repeats))}
    edit.write_json(root / "summary.json", decode_json_object(report))
    return 0 if stable and all(pair["comparable"] is True for pair in pairs) and all(row["passed"] is True for row in rows) else 1


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-dir", type=Path)
    parser.add_argument("--config", choices=tuple(SCENARIOS), default="B1")
    parser.add_argument("--scenario", action="append", choices=SCENARIOS["combined"])
    parser.add_argument("--repeats", type=int, default=2)
    parser.add_argument("--timeout", type=float, default=90)
    parser.add_argument("--budget", type=float, default=300)
    parser.add_argument("--max-turns", type=int, default=10)
    parser.add_argument("--seed", type=int, default=123456789)
    parser.add_argument("--kobold-base-url", default="http://127.0.0.1:5001/v1")
    parser.add_argument("--evidence", type=Path, action="append", default=[])
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--plan", action="store_true")
    mode.add_argument("--diagnostics", action="store_true")
    parser.add_argument("--worker-case", choices=SCENARIOS["combined"], help=argparse.SUPPRESS)
    parser.add_argument("--worker-arm", choices=("A", "B"), help=argparse.SUPPRESS)
    parser.add_argument("--expected-model", help=argparse.SUPPRESS)
    args = parser.parse_args()
    if min(args.repeats, args.timeout, args.budget, args.max_turns) <= 0:
        parser.error("repeats, timeout, budget and max-turns must be positive")
    if args.scenario and len(args.scenario) != len(set(args.scenario)):
        parser.error("duplicate scenarios are not allowed; use --repeats")
    if args.config in PAGE_RESET_CONFIGS and args.scenario and args.scenario != ["page_reset"]:
        parser.error(f"{args.config} only runs page_reset")
    if args.plan:
        features = {PAGE_RESET_GATE: PAGE_RESET_CHECK,
                    PAGE_RESET_NAVIGATION: f"{PAGE_RESET_CHECK}; B schedules the second navigation"}
        print(json.dumps({"config": args.config, "scenarios": args.scenario or SCENARIOS[args.config],
            "units": 2 * args.repeats * len(args.scenario or SCENARIOS[args.config]),
            "diagnostic_scenarios": sorted(DIAGNOSTIC_SCENARIOS),
            "features": features[args.config] if args.config in features else str(surface.CONFIGS[args.config]),
            "combination_requires": ["B1", "B2", "B3", "B4"]}, indent=2))
        return 0
    if args.run_dir is None:
        parser.error("--run-dir is required")
    if args.diagnostics:
        return diagnostics(args.run_dir.resolve())
    if args.config == "combined" and not args.worker_case and len(args.evidence) != 4:
        parser.error("combined requires four --evidence directories, one for each independent B1-B4 run")
    target = gate.select_target(args.timeout, args.kobold_base_url)
    target = Target(target.base_url, target.model, args.seed, args.timeout)
    if args.worker_case:
        if not args.worker_arm or args.expected_model != target.model:
            parser.error("worker requires a pinned arm and unchanged model")
        try:
            run_arm(args.run_dir.resolve(), args.worker_case, args.worker_arm, args.config, target, max_turns=args.max_turns)
            return 0
        except Exception:
            traceback.print_exc()
            return 2
    try:
        return matrix(args, target)
    except (ValueError, FileNotFoundError) as exc:
        parser.error(str(exc))


if __name__ == "__main__":
    raise SystemExit(main())
