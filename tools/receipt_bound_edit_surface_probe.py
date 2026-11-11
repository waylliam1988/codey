"""Serial Gemma4/Chromium A/B across production surfaces.

Only experiment files are changed. --mechanical-b adds typed browser helpers,
receipt repair and factual delivery; it is a bundle, not an edit-only experiment.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys
import traceback
from pathlib import Path
from unittest.mock import patch

from codey.runtime.core import cancellation
from codey.types.json_value import JsonObject, JsonValue, decode_json_object
from tools import mechanical_surface
from tools import receipt_bound_edit_ab as edit
from tools.browser_production_live_gate import run_case, select_target
from tools.completion_artifact_audit import tool_receipts
from tools.completion_explanation_ab import Target, provider_metrics

VERSION = "read-object-full-surface-v1"
SCENARIOS = ("basic", "structured", "legacy_structured", "hybrid", "research", "snapshot_restore",
             "reread", "stale_ref", "stale_edit", "upload", "download", "source_failure",
             "upload_bound", "source_failure_fixed")


def upload_receipt_report(directory: Path) -> JsonObject:
    """Diagnostic only: derive facts from original receipts, never rewrite done."""
    uploads: list[tuple[str, JsonObject]] = []
    reads: list[tuple[str, str]] = []
    for receipt in tool_receipts(directory / "state"):
        result = receipt.result
        value = result.canonical.get("value")
        if result.ok and result.call.name == "browser_upload" and isinstance(value, dict):
            uploads.append((receipt.identity, decode_json_object(value)))
        observation = result.canonical.get("research_observation")
        if (uploads and result.ok and result.call.name == "browser_read" and isinstance(observation, dict)
                and isinstance(text := observation.get("text"), str)):
            reads.append((receipt.identity, text))
    if len(uploads) != 1 or len(reads) != 1:
        return {"available": False, "reason": "requires one successful upload and one subsequent source read"}
    identity, upload = uploads[0]
    return {"available": True, "filename": upload["name"], "sha256": upload["sha256"], "bytes": upload["bytes"],
            "page_text": reads[0][1], "upload_ref": identity, "read_ref": reads[0][0],
            "effect_verified": upload["effect_verified"],
            "scope": "Diagnostic extraction; file-input selection and page display, not server acceptance or delivered answer"}


def scenario_options(scenario: str) -> tuple[str, dict[str, bool]]:
    if scenario not in SCENARIOS:
        raise ValueError("unknown scenario: " + scenario)
    mode = "hybrid" if scenario in {"hybrid", "stale_edit"} else "research" if scenario == "research" else "chat"
    return mode, {
        "strict_research": scenario == "research", "restore_snapshots": scenario == "snapshot_restore",
        "reread": scenario == "reread", "explanation_output": scenario == "structured",
        "structured": scenario == "legacy_structured", "stale_ref": scenario == "stale_ref",
        "stale_edit": scenario == "stale_edit", "preserve_external_field": scenario == "stale_edit",
        "upload_file": scenario in {"upload", "upload_bound"}, "assembled_upload": scenario == "upload_bound",
        "download_file": scenario == "download", "source_failure": scenario in {"source_failure", "source_failure_fixed"},
        "source_attempt_only": scenario in {"source_failure", "source_failure_fixed"},
        "fixed_source_attempt": scenario == "source_failure_fixed",
    }


def run_arm(directory: Path, scenario: str, arm: str, target: Target, *, max_turns: int,
            mechanical: bool = False) -> JsonObject:
    before = edit.production_source_digest()
    mode, options = scenario_options(scenario)
    # Optional second-model review is outside this edit/browser probe. None
    # reports review unavailable; it never manufactures a positive review.
    with (edit.prototype_helper(directory, enabled=arm == "B"),
          mechanical_surface.prototype(directory, scenario, enabled=mechanical and arm == "B"),
          patch("codey.app.provider_services.provider_tab_availability", return_value={}),
          patch("codey.app.review_service.run_review", return_value=None)):
        row = decode_json_object(run_case(directory, mode, target, max_turns=max_turns, **options))
    after = edit.production_source_digest()
    row.update(scenario=scenario, arm=arm, source_before=before, source_after=after,
               source_stable=before == after, passed=row.get("passed") is True and before == after,
               acquisition="scripted-open-url" if scenario == "source_failure_fixed" else "autonomous-model",
               **edit.helper_metrics(directory), **provider_metrics(directory / "provider.jsonl"))
    row.update(mechanical=mechanical and arm == "B", **mechanical_surface.metrics(directory))
    if mechanical and arm == "B" and scenario == "source_failure":
        row["acquisition"] = "mechanical-required-open-url"
    if scenario in {"upload", "upload_bound"} and (directory / "state" / "durable.sqlite3").exists():
        report = upload_receipt_report(directory)
        edit.write_json(directory / "receipt-report.json", report)
        row["receipt_report_available"] = report["available"]
    edit.write_json(directory / "summary.json", row)
    return row


def run_worker(directory: Path, scenario: str, arm: str, target: Target, *, budget: float,
               max_turns: int, mechanical: bool = False) -> JsonObject:
    command = [sys.executable, "-m", "tools.receipt_bound_edit_surface_probe", "--run-dir", str(directory),
               "--worker-case", scenario, "--worker-arm", arm, "--expected-model", target.model,
               "--kobold-base-url", target.base_url, "--timeout", str(target.timeout),
               "--max-turns", str(max_turns), "--budget", str(budget)]
    if mechanical:
        command.append("--mechanical-b")
    process, owner = cancellation.start_process(command, cwd=edit.ROOT)
    error = ""
    output = ""
    try:
        completed = cancellation.wait_process(process, owner, command, budget, capture_limit_bytes=100_000)
        output = completed.stdout + completed.stderr
        if completed.returncode:
            error = f"worker exited {completed.returncode}"
    except subprocess.TimeoutExpired:
        error = f"worker exceeded {budget}s; owned process tree terminated"
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "worker.log").write_text(output + error, encoding="utf-8")
    summary = directory / "summary.json"
    if not error and summary.exists():
        return decode_json_object(json.loads(summary.read_text(encoding="utf-8")))
    row: JsonObject = {"scenario": scenario, "arm": arm, "passed": False, "error": error or "missing worker summary"}
    # Preserve an incomplete worker's original result, rather than overwriting it.
    edit.write_json(directory / "worker-failure.json", row)
    return row


def pair_audit(pair: Path, rows: list[JsonObject]) -> JsonObject:
    tasks = [pair / arm / "task.txt" for arm in ("A", "B")]
    identical = all(path.exists() for path in tasks) and tasks[0].read_bytes() == tasks[1].read_bytes()
    digests = [row.get(key) for row in rows for key in ("source_before", "source_after")]
    stable = len(rows) == 2 and all(isinstance(value, str) for value in digests) and len(set(map(str, digests))) == 1
    return {"pair": pair.name, "identical_task_text": identical, "source_comparable": stable,
            "comparable": identical and stable,
            "A_passed": next((row["passed"] for row in rows if row["arm"] == "A"), False),
            "B_passed": next((row["passed"] for row in rows if row["arm"] == "B"), False)}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-dir", type=Path, required=True)
    parser.add_argument("--scenario", choices=(*SCENARIOS, "all"), default="all")
    parser.add_argument("--repeats", type=int, default=1)
    parser.add_argument("--timeout", type=float, default=90, help="Per model request timeout.")
    parser.add_argument("--budget", type=float, default=300, help="Total seconds per isolated worker.")
    parser.add_argument("--max-turns", type=int, default=10)
    parser.add_argument("--mechanical-b", action="store_true", help="B additionally uses typed browser helpers, receipt-only repair and factual delivery.")
    parser.add_argument("--kobold-base-url", default="http://127.0.0.1:5001/v1")
    parser.add_argument("--worker-case", choices=SCENARIOS, help=argparse.SUPPRESS)
    parser.add_argument("--worker-arm", choices=("A", "B"), help=argparse.SUPPRESS)
    parser.add_argument("--expected-model", help=argparse.SUPPRESS)
    args = parser.parse_args()
    if min(args.repeats, args.max_turns, args.timeout, args.budget) <= 0:
        parser.error("repeats, max-turns, timeout and budget must be positive")
    target = select_target(args.timeout, args.kobold_base_url)
    if args.worker_case:
        if not args.worker_arm or target.model != args.expected_model:
            parser.error("worker requires a pinned arm and unchanged model")
        try:
            row = run_arm(args.run_dir.resolve(), args.worker_case, args.worker_arm, target,
                          max_turns=args.max_turns, mechanical=args.mechanical_b)
            print(json.dumps(row, ensure_ascii=True), flush=True)
        except Exception:
            traceback.print_exc()
            return 2
        return 0  # Model failures are scored from summary, not discarded as process failures.
    root = args.run_dir.resolve()
    root.mkdir(parents=True, exist_ok=False)
    before = edit.production_source_digest()
    selected = SCENARIOS if args.scenario == "all" else (args.scenario,)
    files = [Path(__file__), Path(edit.__file__), edit.ROOT / "tools/browser_production_live_gate.py",
             edit.ROOT / "tools/completion_explanation_ab.py", edit.ROOT / "tools/receipt_bound_edit_recovery_probe.py"]
    if args.mechanical_b:
        files.append(Path(mechanical_surface.__file__))
    edit.write_json(root / "manifest.json", {
        "version": mechanical_surface.VERSION if args.mechanical_b else VERSION, "scenarios": list(selected), "repeats": args.repeats,
        "model": target.model, "base_url": target.base_url, "seed": target.seed, "temperature": 0,
        "provider": "Shared ExperimentProvider/text JSON schema; B adds scripted repair and required-source acquisition"
                    if args.mechanical_b else "ExperimentProvider; text JSON schema; both arms identical",
        "max_turns": args.max_turns,
        "request_timeout": args.timeout, "worker_budget": args.budget,
        "production_source_before": before, "source_digest_kind": "codey-files-sha256-v2",
        "interface_version": edit.INTERFACE_VERSION, "helper_sha256": edit.HELPER_DIGEST,
        "mechanical_b": args.mechanical_b, "mechanical_version": mechanical_surface.VERSION if args.mechanical_b else None,
        "mechanical_helper_sha256": mechanical_surface.HELPER_DIGEST if args.mechanical_b else None,
        "harnesses": {path.name: hashlib.sha256(path.read_bytes()).hexdigest() for path in files},
        "optional_model_review": "unavailable; not scored as positive review", "secondary_provider_tabs": False,
        "diagnostics": {"upload_bound": "Production binder in A; experiment source-text selector in B" if args.mechanical_b else
                        "Existing production json_explanation + receipt binding in both arms",
                        "source_failure_fixed": "Scripted first CodeMode/open_url; real acquisition, not autonomous tool selection"},
        "snapshot_scope": "Serialize/restore frozen turn contracts; not a whole-process restart",
        "limits": "Controlled Chromium page, one pair per scenario by default; not proof of general model reliability",
    })
    rows: list[JsonObject] = []
    pairs: list[JsonValue] = []
    for repetition in range(args.repeats):
        for index, scenario in enumerate(selected):
            pair = root / f"{repetition + 1:02d}-{scenario}"
            pair.mkdir()
            pair_rows: list[JsonObject] = []
            for arm in (("A", "B") if (index + repetition) % 2 == 0 else ("B", "A")):
                print(json.dumps({"starting": scenario, "arm": arm, "repetition": repetition + 1}), flush=True)
                row = run_worker(pair / arm, scenario, arm, target, budget=args.budget,
                                 max_turns=args.max_turns, mechanical=args.mechanical_b)
                rows.append(row)
                pair_rows.append(row)
                print(json.dumps({"scenario": scenario, "arm": arm, "passed": row["passed"],
                                  "stop_reason": row.get("stop_reason"), "error": row.get("error")}), flush=True)
                edit.write_json(root / "progress.json", {"runs": list(rows), "pairs": pairs})
            pairs.append(pair_audit(pair, pair_rows))
    after = edit.production_source_digest()
    summary: JsonObject = {"version": mechanical_surface.VERSION if args.mechanical_b else VERSION,
                           "source_before": before, "source_after": after,
                           "source_stable": before == after, "total_runs": len(rows),
                           "passed_runs": sum(row["passed"] is True for row in rows), "runs": list(rows), "pairs": pairs}
    edit.write_json(root / "summary.json", summary)
    return 0 if before == after and all(row["passed"] is True for row in rows) else 1


if __name__ == "__main__":
    raise SystemExit(main())
