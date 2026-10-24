"""Observe real local-model requests through the production kernel and tools.

Only the temporary workspace and task requirements are fixture data. Provider,
CodeMode worker, child tools, commits, completion and recovery are unchanged.
"""
from __future__ import annotations

import argparse
import json
import subprocess
from contextlib import closing
from pathlib import Path
from types import SimpleNamespace

from codey.operations.completion_gate import evaluate
from codey.operations.kernel_session_recovery import restore_task_session
from codey.operations.recovery import record_entry_policy, recover_effects_for_resume
from codey.operations.task_effects import KernelRecordedProvider
from codey.operations.task_loop import (
    KernelExecutionDeps,
    KernelObservationDeps,
    KernelRunRequest,
    KernelTransportDeps,
    run_task_kernel,
)
from codey.operations.task_session import TaskSession
from codey.policies.task_policy import TaskPolicy
from codey.providers.local_config import LocalContextBudget, LocalProviderConfig
from codey.providers.local_connection import connect_local
from tests.test_session_log_receipt_recovery_preserves_facts import _dirs, _open_runtime
from tools.local_model_gate_attempts import record_api_generations


def probe(directory: Path, *, base_url: str, model: str, protocol: str, case: str) -> dict:
    project, state, logdir = _dirs(directory)
    (project / "a.txt").write_text("AMBER-471\n", encoding="utf-8")
    (project / "b.txt").write_text("COBALT-923\n", encoding="utf-8")
    _, mutations, managed, revisions, deps, sink = _open_runtime(logdir, state, "s", "r", project)
    writing = case == "write"
    task = (
        "Use codemode to create created.txt containing exactly SAPPHIRE-369 followed by a newline. "
        "Do not run tests, lint, shell commands or any other verification commands. Report what you actually created."
        if writing else
        'Use codemode to read a.txt and b.txt. Return exactly a JSON object with keys "a" and "b" '
        "containing their observed first lines. Do not modify files."
    )
    policy = TaskPolicy(frozenset({"control", "project.read"} | ({"project.write"} if writing else set())),
        required_checks=("tool_succeeded:codemode",), denied_capabilities=frozenset({"project.verify"}))
    record_entry_policy(mutations, session_id="s", run_id="r", policy=policy)
    session = TaskSession(policy=policy, project=str(project), task_text=task, max_turns=8,
                          project_changes_required=writing, verification_forbidden=True)
    config = LocalProviderConfig(base_url=base_url, model=model, api_key="local", native_tools_mode=protocol,
                                 context=LocalContextBudget(32768, 1024, 12000), thinking_enabled=False)
    try:
        with record_api_generations(directory / "provider.jsonl"), closing(connect_local(config=config)) as provider:
            result = run_task_kernel(session, request=KernelRunRequest(
                transport=KernelTransportDeps(provider=KernelRecordedProvider(provider, sink), run_id="r",
                    effect_scope="task", provider_id="local", user_task=task),
                execution=KernelExecutionDeps(project_path=project, managed_outputs=managed, session_id="s",
                    durable=mutations.durable, workspace_revision_store=revisions,
                    permission_profile="coding_writer" if writing else "planning_readonly"),
                observation=KernelObservationDeps(intent_sink=sink),
            ))
        requests = [json.loads(line)["payload"] for line in (directory / "provider.jsonl").read_text(
                    encoding="utf-8").splitlines() if json.loads(line).get("type") == "request"]
        initial = requests[0]
        prompt = "\n".join(str(item.get("content", "")) for item in initial.get("messages", []))
        visible = next(line.removeprefix("Visible tools: ").split(", ") for line in prompt.splitlines()
                       if line.startswith("Visible tools: "))
        schema_names = {item["function"]["name"] for item in initial.get("tools", [])}
        wire_consistent = (schema_names == set(visible) and not {"read_files", "parallel"} & schema_names
                           if protocol == "on" else not schema_names)
        calls_before_recovery = dict(session.executed)
        recovered = recover_effects_for_resume(deps, session_id="s", run_id="r", project=str(project), task_kind="project")
        fresh = TaskSession(policy=policy, project=str(project), project_changes_required=writing, verification_forbidden=True)
        frame = SimpleNamespace(run_id="r", recovered_tool_outcomes=recovered.recovered_tool_outcomes,
            settled_tool_outcomes=recovered.settled_tool_outcomes,
            recovered_tool_result_batch_id=recovered.recovered_tool_result_batch_id)
        restore_task_session(frame, fresh)
        recovered_complete = evaluate(fresh, result.summary).complete
        count = len(fresh.executed)
        restore_task_session(frame, fresh)
        stable = len(fresh.executed) == count and session.executed == calls_before_recovery
        if writing:
            target = project / "created.txt"
            valid_output = target.is_file() and target.read_text(encoding="utf-8") == "SAPPHIRE-369\n"
        else:
            try:
                valid_output = json.loads(result.summary) == {"a": "AMBER-471", "b": "COBALT-923"}
            except ValueError:
                valid_output = False
        child_tool = "edit" if writing else "read_file"
        observed_children = [record for record in session.executed.values() if record.get("name") == child_tool
                             and record.get("ok") is True and ":child:" in str(record.get("call_id", ""))]
        workflow_observed = len(observed_children) >= (1 if writing else 2)
        row = {"case": case, "protocol": protocol,
            "ok": result.completed and valid_output and workflow_observed and wire_consistent
                  and recovered.ok and recovered_complete and stable,
            "stop_reason": result.stop_reason, "summary": result.summary, "valid_output": valid_output,
            "wire_contract_consistent": wire_consistent, "visible_tools": visible, "schema_tools": sorted(schema_names),
            "executed": list(session.executed.values()), "recovered_completion_passed": recovered_complete,
            "repeat_recovery_stable": stable, "required_child_workflow_observed": workflow_observed}
        (directory / "result.json").write_text(json.dumps(row, ensure_ascii=False, indent=2), encoding="utf-8")
        return row
    finally:
        mutations.durable.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--model", required=True)
    parser.add_argument("--base-url", default="http://127.0.0.1:5001/v1")
    parser.add_argument("--protocols", nargs="+", choices=("on", "off"), default=("on", "off"))
    parser.add_argument("--cases", nargs="+", choices=("read", "write"), default=("read", "write"))
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=False)
    (args.output / "production.patch").write_bytes(subprocess.check_output(["git", "diff", "--binary", "--", "codey"]))
    rows = []
    for protocol in args.protocols:
        for case in args.cases:
            directory = args.output / f"{protocol}-{case}"
            try:
                row = probe(directory, base_url=args.base_url, model=args.model, protocol=protocol, case=case)
            except Exception as exc:
                row = {"protocol": protocol, "case": case, "ok": False, "error": repr(exc)}
            rows.append(row)
            print(json.dumps(row, ensure_ascii=False), flush=True)
            (args.output / "summary.json").write_text(json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8")
    return 0 if all(row["ok"] for row in rows) else 1


if __name__ == "__main__":
    raise SystemExit(main())
