"""Offline edit safety checks; opt in to serial model A/B with --live.

The experimental interface is imported from receipt_bound_edit_ab. No production
source, prompt template, write executor or worktree is changed.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from collections.abc import Sequence
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from codey.agents.tools import AgentToolFns
from codey.codemode.kernel import build_execute_turn_executor
from codey.operations import kernel_protocol
from codey.operations.kernel_execution import execute_turn
from codey.operations.task_session import TaskSession
from codey.policies.task_policy import TaskPolicy
from codey.runtime.core.models import ToolCall, ToolResult
from codey.toolchain import runtime as tool_runtime
from codey.toolchain.constants import STALE_READ_REFERENCE_ERROR
from codey.toolchain.runtime import ToolOutcome
from codey.types.json_value import JsonObject, JsonValue, decode_json_object
from codey.workspace.revision import WorkspaceRevisionStore
from tools import receipt_bound_edit_ab as ab
from tools.completion_artifact_audit import tool_receipts
from tools.local_model_gate_attempts import GateTarget
from tools.shell_web_diagnostic_gate import select_target

PROBE_VERSION = "read-object-stale-recovery-v1"
EXTERNAL_FIELD: JsonObject = {"revision": 1, "marker": "EXTERNAL_CHANGE_MUST_SURVIVE"}


def external_content(initial: JsonObject) -> JsonObject:
    return {**initial, "external_revision": EXTERNAL_FIELD}


def receipt_audit(receipts: Sequence[tuple[str, ToolResult]]) -> JsonObject:
    """Only an earlier, complete, same-path read can count as a stale conflict."""
    reads: dict[str, ToolResult] = {}
    read_positions: dict[str, int] = {}
    rows: list[JsonValue] = []
    stale_refs: list[JsonValue] = []
    successful_refs: list[JsonValue] = []
    first_rejection: JsonObject | None = None
    first_tool_rejection: JsonObject | None = None
    last_stale_position = -1
    last_stale_path: object = None
    recovered = False
    for position, (identity, result) in enumerate(receipts):
        row: JsonObject = {"identity": identity, "tool": result.call.name, "ok": result.ok,
                           "args": decode_json_object(result.call.args), "model_text": result.model_text,
                           "canonical": decode_json_object(result.canonical), "audit": decode_json_object(result.audit)}
        rows.append(row)
        if not result.ok and first_tool_rejection is None:
            first_tool_rejection = row
        if result.call.name == "read_file" and result.ok:
            value = result.canonical.get("value")
            if isinstance(value, dict) and value.get("text_complete") is True:
                reads[identity] = result
                read_positions[identity] = position
        if result.call.name != "edit":
            continue
        ref = result.call.args.get("base_result_ref")
        read = reads.get(ref) if isinstance(ref, str) else None
        valid_read = read is not None and read.call.args.get("path") == result.call.args.get("path")
        stale = not result.ok and valid_read and STALE_READ_REFERENCE_ERROR in result.model_text
        row["valid_original_read"] = valid_read
        row["true_stale_conflict"] = stale
        if not result.ok and first_rejection is None:
            first_rejection = row
        if stale:
            stale_refs.append(ref)
            last_stale_position = position
            last_stale_path = result.call.args.get("path")
        if result.ok:
            successful_refs.append(ref)
            if (valid_read and isinstance(ref, str) and last_stale_position >= 0
                    and read_positions[ref] > last_stale_position
                    and result.call.args.get("path") == last_stale_path):
                recovered = True
    return {"receipts": rows, "first_edit_rejection": first_rejection,
            "first_tool_rejection": first_tool_rejection,
            "true_stale_conflicts": len(stale_refs), "stale_read_refs": stale_refs,
            "successful_edit_refs": successful_refs,
            "fresh_read_recovery": recovered}


def fixed_program(arm: str, *, invalid_read: bool = False) -> str:
    common = ("const r=await tools.read_file({path:'result.json'});"
              "const data=JSON.parse(unwrap(r).text); data.amount=173; data.code='QUARTZ_851';"
              "const content=JSON.stringify(data);")
    if arm == "A":
        ref = "unwrap(r).result_ref" if invalid_read else "r.result_ref"
        return common + f"return unwrap(await tools.edit({{path:'result.json',content,base_result_ref:{ref}}}));"
    read = "unwrap(r)" if invalid_read else "r"
    return common + f"return unwrap(await tools.edit({{read:{read},content}}));"


def run_deterministic_case(directory: Path, arm: str, scenario: str) -> JsonObject:
    directory.mkdir(parents=True, exist_ok=False)
    project = directory / "project"
    project.mkdir()
    case = ab.CASES[0]
    target = project / "result.json"
    ab.write_json(target, case.initial)
    backup = project / "result-backup.json"
    ab.write_json(backup, case.initial)
    backup_before = backup.read_bytes()
    session = TaskSession(TaskPolicy(frozenset({"control", "project.read", "project.write"})), project=str(project))
    revisions = WorkspaceRevisionStore(directory / "revisions")
    read_calls = 0

    def concurrent_read(root: Path, path: str, *, offset: int = 1, limit: int = 300) -> ToolOutcome:
        nonlocal read_calls
        outcome = tool_runtime.read_file(root, path, offset=offset, limit=limit)
        read_calls += 1
        if scenario == "stale-recovery" and read_calls == 1:
            ab.write_json(target, external_content(case.initial))
        return outcome

    stages: list[JsonValue] = []
    with ab.prototype_helper(directory, enabled=arm == "B"):
        for turn in range(1, 3 if scenario == "stale-recovery" else 2):
            snapshot = kernel_protocol.build_turn_snapshot(session)
            executor = build_execute_turn_executor(
                session=session, snapshot=snapshot, run_id="recovery-probe", effect_scope="task", turn=turn,
                project_path=project, allow_writes=True, workspace_revision_store=revisions,
                tool_fns=AgentToolFns(read_file=concurrent_read),
            )
            result = execute_turn(
                session, [ToolCall("codemode", {"code": fixed_program(arm, invalid_read=scenario == "invalid-read")})],
                executors={"codemode": executor}, run_id="recovery-probe", effect_scope="task", turn=turn,
                project_path=project, snapshot=snapshot, workspace_revision_store=revisions,
            )[0]
            stages.append({"turn": turn, "ok": result.ok, "model_text": result.model_text,
                           "file": decode_json_object(json.loads(target.read_text(encoding="utf-8")))})
    audit = receipt_audit(list(session._memory_results.items()))
    ab.write_json(directory / "receipts.json", audit)
    ab.write_json(directory / "stages.json", stages)
    expected = case.initial if scenario == "invalid-read" else case.expected
    if scenario == "stale-recovery":
        expected = external_content(expected)
    actual = decode_json_object(json.loads(target.read_text(encoding="utf-8")))
    first = decode_json_object(stages[0])
    edits = [result for result in session._memory_results.values() if result.call.name == "edit"]
    if scenario == "stale-recovery":
        behavior = (first["ok"] is False and first["file"] == external_content(case.initial)
                    and decode_json_object(stages[1])["ok"] is True
                    and read_calls == 2 and len(edits) == 2
                    and audit["true_stale_conflicts"] == 1 and audit["fresh_read_recovery"] is True)
    elif scenario == "invalid-read":
        behavior = first["ok"] is False and read_calls == 1 and not any(edit.ok for edit in edits)
    else:
        behavior = first["ok"] is True and read_calls == 1 and len(edits) == 1 and edits[0].ok
    preserved = backup.read_bytes() == backup_before
    summary: JsonObject = {"arm": arm, "scenario": scenario, "mode": "fixed-program",
                           "passed": behavior and actual == expected and preserved,
                           "target_correct": actual == expected, "backup_preserved": preserved,
                           "read_calls": read_calls, "edit_calls": len(edits),
                           "true_stale_conflicts": audit["true_stale_conflicts"],
                           "fresh_read_recovery": audit["fresh_read_recovery"]}
    ab.write_json(directory / "summary.json", summary)
    return summary


def run_live_arm(directory: Path, case: ab.EditCase, arm: str, target: GateTarget,
                 *, max_turns: int) -> JsonObject:
    injected = False
    project = directory / "project"

    def observe(event: JsonObject) -> None:
        nonlocal injected
        path = event.get("path")
        if (injected or event.get("type") != "tool" or event.get("tool_name") != "read_file"
                or event.get("ok") is not True or not isinstance(path, str)
                or (project / path).resolve() != (project / "result.json").resolve()):
            return
        # Read the committed receipt, rather than treating every successful partial read as complete.
        committed = tool_receipts(directory / "state")
        if not any(row.result.call.name == "read_file" and row.result.ok
                   and isinstance(value := row.result.canonical.get("value"), dict)
                   and value.get("text_complete") is True and row.result.call.args.get("path") == path
                   for row in committed):
            return
        before = decode_json_object(json.loads((project / "result.json").read_text(encoding="utf-8")))
        after = external_content(before)
        ab.write_json(project / "result.json", after)
        injected = True
        ab.write_json(directory / "injection.json", {"trigger": event, "before": before, "after": after})

    # This is an edit/recovery probe. Report optional review as unavailable,
    # rather than connecting a second model or manufacturing a passing review.
    with (patch("codey.app.provider_services.provider_tab_availability", return_value={}),
          patch("codey.app.review_service.run_review", return_value=None)):
        row = ab.run_arm(directory, replace(case, expected=external_content(case.expected)), arm, target,
                         max_turns=max_turns, task=ab.common_task(case), on_event=observe)
    state = directory / "state"
    audit = receipt_audit([(receipt.identity, receipt.result) for receipt in tool_receipts(state)]) if (
        state / "durable.sqlite3").exists() else receipt_audit([])
    ab.write_json(directory / "receipts.json", audit)
    source_stable = row["source_before"] == row["source_after"]
    row.update(mode="autonomous-model", injection_occurred=injected, source_stable=source_stable,
               true_stale_conflicts=audit["true_stale_conflicts"], fresh_read_recovery=audit["fresh_read_recovery"],
               first_edit_rejection=audit["first_edit_rejection"],
               first_tool_rejection=audit["first_tool_rejection"],
               passed=row["passed"] is True and injected and source_stable
               and audit["true_stale_conflicts"] == 1 and audit["fresh_read_recovery"] is True)
    ab.write_json(directory / "summary.json", row)
    return row


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-dir", type=Path, required=True)
    parser.add_argument("--live", action="store_true", help="Call the selected local model; defaults to offline checks.")
    parser.add_argument("--repeats", type=int, default=1)
    parser.add_argument("--timeout", type=float, default=240)
    parser.add_argument("--max-turns", type=int, default=8)
    parser.add_argument("--kobold-base-url", default="http://127.0.0.1:5001/v1")
    parser.add_argument("--worker-case", choices=[case.name for case in ab.CASES], help=argparse.SUPPRESS)
    parser.add_argument("--worker-arm", choices=("A", "B"), help=argparse.SUPPRESS)
    parser.add_argument("--expected-model", help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.repeats < 1 or args.max_turns < 1 or args.timeout <= 0:
        parser.error("repeats, max-turns and timeout must be positive")
    if args.worker_case:
        if not args.live or not args.worker_arm or not args.expected_model:
            parser.error("workers require --live, --worker-arm and --expected-model")
        worker_target = select_target("gemma", args.timeout, args.kobold_base_url)
        if worker_target.model != args.expected_model:
            parser.error("selected model changed; this pair is not comparable")
        case = next(case for case in ab.CASES if case.name == args.worker_case)
        run_live_arm(args.run_dir.resolve(), case, args.worker_arm, worker_target, max_turns=args.max_turns)
        return 0  # The parent scores the written summary, including failed model attempts.
    root = args.run_dir.resolve()
    root.mkdir(parents=True, exist_ok=False)
    source_before = ab.production_source_digest()
    target = select_target("gemma", args.timeout, args.kobold_base_url) if args.live else None
    ab.write_json(root / "manifest.json", {
        "probe_version": PROBE_VERSION, "interface_version": ab.INTERFACE_VERSION,
        "helper_sha256": ab.HELPER_DIGEST,
        "harness_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        "shared_harness_sha256": hashlib.sha256(Path(ab.__file__).read_bytes()).hexdigest(),
        "mode": "autonomous-model" if args.live else "fixed-program", "task_identical_across_arms": True,
        "production_source_at_start": source_before, "source_digest_kind": "codey-files-sha256-v2",
        "model": target.model if target else None, "base_url": target.base_url if target else None,
        "temperature": target.temperature if target else None, "repeats": args.repeats,
        "protocol": target.protocol if target else None, "api_protocol": target.api_protocol if target else None,
        "secondary_provider_tabs": False,
        "optional_model_review": "unavailable_in_this_edit_probe",
        "timeout": args.timeout, "max_turns": args.max_turns,
        "limits": "Offline programs verify mechanics only. Live mode tests edits without browser/upload/source workflows.",
    })
    rows: list[JsonObject] = []
    if target is None:
        for scenario in ("clean", "invalid-read", "stale-recovery"):
            for arm in ("A", "B"):
                rows.append(run_deterministic_case(root / scenario / arm, arm, scenario))
    else:
        for repetition in range(args.repeats):
            for index, case in enumerate(ab.CASES):
                pair = root / f"{repetition + 1:02d}-{case.name}"
                pair.mkdir()
                for arm in (("A", "B") if (repetition + index) % 2 == 0 else ("B", "A")):
                    print(json.dumps({"starting": case.name, "arm": arm, "repetition": repetition + 1}), flush=True)
                    rows.append(ab.run_worker(
                        pair / arm, case, arm, base_url=args.kobold_base_url, timeout=args.timeout,
                        max_turns=args.max_turns, worker_module="tools.receipt_bound_edit_recovery_probe",
                        worker_args=("--live", "--expected-model", target.model),
                    ))
                    ab.write_json(root / "progress.json", {"runs": list(rows)})
    source_after = ab.production_source_digest()
    source_stable = source_before == source_after and all(
        row.get("source_before", source_before) == source_before
        and row.get("source_after", source_before) == source_before for row in rows)
    summary: JsonObject = {"probe_version": PROBE_VERSION, "mode": "autonomous-model" if args.live else "fixed-program",
                           "passed": source_stable and all(row["passed"] is True for row in rows),
                           "passed_runs": sum(row["passed"] is True for row in rows), "total_runs": len(rows),
                           "production_source_before": source_before, "production_source_after": source_after,
                           "source_stable": source_stable, "runs": list(rows)}
    ab.write_json(root / "summary.json", summary)
    print(json.dumps(summary, ensure_ascii=True), flush=True)
    return 0 if summary["passed"] is True else 1


if __name__ == "__main__":
    raise SystemExit(main())
