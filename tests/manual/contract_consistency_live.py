"""Observe real local-model requests through the production kernel and tools.

Only the temporary workspace and task requirements are fixture data. Provider,
CodeMode worker, child tools, commits, completion and recovery are unchanged.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import threading
import time
import zipfile
from contextlib import closing
from pathlib import Path
from types import SimpleNamespace

from codey.operations.completion_gate import evaluate
from codey.operations.recovery import record_entry_policy
from codey.operations.task_loop import (
    KernelExecutionDeps,
    KernelObservationDeps,
    KernelRunRequest,
    KernelTransportDeps,
    run_task_kernel,
)
from codey.operations.task_session import TaskSession
from codey.policies.task_policy import TaskPolicy
from codey.policies.tool_requirement import require_tool_success
from codey.providers.local_config import LocalContextBudget, LocalProviderConfig
from codey.providers.local_connection import connect_local
from tests.support.receipt_fixtures import inspect_receipt_facts
from tests.support.session_projection import restore_task_session
from tests.test_session_log_receipt_recovery_preserves_facts import _dirs, _open_runtime
from tools.local_model_gate_attempts import record_api_generations


def probe(directory: Path, *, base_url: str, model: str, protocol: str, case: str,
          seed: int = 41, temperature: float = 0.3) -> dict:
    started = time.monotonic()
    project, state, logdir = _dirs(directory)
    (project / "a.txt").write_text("AMBER-471\n", encoding="utf-8")
    (project / "b.txt").write_text("COBALT-923\n", encoding="utf-8")
    _, mutations, managed, revisions, deps, sink = _open_runtime(logdir, state, "s", "r", project)
    writing = case in {"write", "replace"}
    cancelling = case in {"cancel", "search_cancel"}
    searching = case.startswith("search_")
    stop = threading.Event()
    requests_at_stop: list[int] = []

    def observe(event):
        if cancelling and event.kind == "tool_start" and event.metadata.get("parent_call_id"):
            requests_at_stop.append(sum(json.loads(line).get("type") == "request" for line in
                (directory / "provider.jsonl").read_text(encoding="utf-8").splitlines()))
            stop.set()
    task = (
        "Use codemode to create created.txt containing exactly SAPPHIRE-369 followed by a newline. "
        "Do not run tests, lint, shell commands or any other verification commands. Report what you actually created."
        if writing else
        'Use codemode to read a.txt and b.txt. Return exactly a JSON object with keys "a" and "b" '
        "containing their observed first lines. Do not modify files."
    )
    required = ((require_tool_success("edit", via="codemode", arguments={"path": "created.txt"}),) if writing else
                tuple(require_tool_success("read_file", via="codemode", arguments={"path": path},
                                           answer_pointer=f"/{key}", result_pointer="/text", result_line=1)
                      for key, path in (("a", "a.txt"), ("b", "b.txt"))))
    if case == "replace":
        task = ("Use codemode to read the complete a.txt, then update that existing file with edit content "
                "and base_result_ref from the read result. Its complete new content must be SAPPHIRE-369 "
                "followed by a newline. Do not use replacements. Do not execute tests or shell commands. "
                "Report what you actually updated.")
        required = tuple(require_tool_success(tool, via="codemode", arguments={"path": "a.txt"})
                         for tool in ("read_file", "edit"))
    expected_search = None
    if searching:
        literal_line = '"PATTERN.*"' if case == "search_quoted" else "PATTERN.*"
        if case == "search_context":
            (project / "sample.py").write_text("before\nNEEDLE\nafter\n", encoding="utf-8")
            (project / "ignored.txt").write_text("before\nNEEDLE\nafter\n", encoding="utf-8")
        else:
            (project / "sample.py").write_text(literal_line + "\nSIGNAL-729\n", encoding="utf-8")
            (project / "ignored.txt").write_text("SIGNAL-999\n", encoding="utf-8")
        query = {"search_literal": "PATTERN.*", "search_regex": "SIGNAL-[0-9]{3}",
                 "search_empty": "ABSENT-NEEDLE", "search_cancel": "PATTERN.*",
                 "search_quoted": '"PATTERN.*"', "search_context": "NEEDLE"}[case]
        args = {"query": query, "path": ".", "literal": case != "search_regex",
                "case_sensitive": True, "include": "*.py"}
        if case == "search_context":
            args.update(exclude=["ignored.txt"], before_context=1, after_context=1)
        empty = case == "search_empty"
        expected_search = (
            {"matches": []} if empty else
            {"context": [
                {"path": "sample.py", "line": 1, "text": "before", "relation": "before"},
                {"path": "sample.py", "line": 3, "text": "after", "relation": "after"},
            ]} if case == "search_context" else
            {"text": "SIGNAL-729" if case == "search_regex" else literal_line}
        )
        pointer = "/matches" if empty else "/context" if case == "search_context" else "/text"
        required = (require_tool_success("grep", via="codemode", arguments=args, answer_pointer=pointer,
                                        result_pointer=("/matches" if empty else "/context" if case == "search_context" else "/matches/0/text")),)
        guidance = ("Use codemode tools.grep exactly once. The query value is the bare text NEEDLE; "
            "do not include quote characters in the query value. " if case == "search_context" else
            "Use codemode tools.grep exactly once. ")
        task = (guidance + "Use these arguments literally: " + json.dumps(args) +
                ('. Return only JSON {"matches": <the observed matches array>}. ' if empty else
                 '. Return only JSON {"context": <the observed context array>}. ' if case == "search_context" else
                 '. Return only JSON {"text": <the complete first matching line text>}. ') +
                "Do not modify files or execute shell/verification commands. Check ok before consuming value.")
    policy = TaskPolicy(frozenset({"control", "project.read"} | ({"project.write"} if writing else set())),
        required_checks=required, denied_capabilities=frozenset({"project.verify"}))
    record_entry_policy(mutations, session_id="s", run_id="r", policy=policy, durable=mutations.durable)
    session = TaskSession(policy=policy, project=str(project), task_text=task, max_turns=8,
                          project_changes_required=writing, verification_forbidden=True)
    config = LocalProviderConfig(base_url=base_url, model=model, api_key="local", native_tools_mode=protocol,
                                 context=LocalContextBudget(32768, 1024, 12000), thinking_enabled=False)
    try:
        with record_api_generations(directory / "provider.jsonl"), closing(connect_local(config=config)) as provider:
            provider.temperature = temperature
            configure = provider.configure_request

            def sampling(payload):
                configure(payload)
                payload["seed"] = seed

            provider.configure_request = sampling
            result = run_task_kernel(session, request=KernelRunRequest(
                transport=KernelTransportDeps(provider=provider, run_id="r",
                    effect_scope="task", provider_id="local", user_task=task, stop_flag=stop),
                execution=KernelExecutionDeps(project_path=project, managed_outputs=managed, session_id="s",
                    durable=mutations.durable, workspace_revision_store=revisions,
                    permission_profile="coding_writer" if writing else "planning_readonly"),
                observation=KernelObservationDeps(intent_sink=sink, on_event=observe),
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
        recovered = inspect_receipt_facts(deps, session_id="s", run_id="r", project=str(project), task_kind="project")
        fresh = TaskSession(policy=policy, project=str(project), project_changes_required=writing, verification_forbidden=True)
        frame = SimpleNamespace(run_id="r", recovered_tool_outcomes=recovered.recovered_tool_outcomes,
            settled_tool_outcomes=recovered.settled_tool_outcomes,
            recovered_tool_result_batch_id=recovered.recovered_tool_result_batch_id)
        restore_task_session(frame, fresh)
        recovered_complete = evaluate(fresh, result.summary).complete
        count = len(fresh.executed)
        restore_task_session(frame, fresh)
        stable = len(fresh.executed) == count and session.executed == calls_before_recovery
        if searching:
            try:
                valid_output = json.loads(result.summary) == expected_search
            except ValueError:
                valid_output = False
        elif writing:
            target = project / ("a.txt" if case == "replace" else "created.txt")
            valid_output = target.is_file() and target.read_text(encoding="utf-8") == "SAPPHIRE-369\n"
        else:
            try:
                valid_output = json.loads(result.summary) == {"a": "AMBER-471", "b": "COBALT-923"}
            except ValueError:
                valid_output = False
        child_tool = "grep" if searching else "edit" if writing else "read_file"
        observed_children = [record for record in session.executed.values() if record.get("name") == child_tool
                             and record.get("ok") is True and ":child:" in str(record.get("call_id", ""))]
        workflow_observed = len(observed_children) >= (1 if writing or searching else 2)
        if cancelling:
            valid_output = ((project / "a.txt").read_text(encoding="utf-8") == "AMBER-471\n"
                            and (project / "b.txt").read_text(encoding="utf-8") == "COBALT-923\n"
                            and not (project / "created.txt").exists())
            workflow_observed = bool(requests_at_stop) and all(count == len(requests) for count in requests_at_stop)
        terminal_valid = (not result.completed and result.stop_reason == "stopped" and not recovered_complete
                          if cancelling else result.completed and recovered_complete)
        row = {"case": case, "protocol": protocol,
            "ok": terminal_valid and valid_output and workflow_observed and wire_consistent and recovered.ok and stable,
            "stop_reason": result.stop_reason, "summary": result.summary, "valid_output": valid_output,
            "wire_contract_consistent": wire_consistent, "visible_tools": visible, "schema_tools": sorted(schema_names),
            "executed": list(session.executed.values()), "recovered_completion_passed": recovered_complete,
            "repeat_recovery_stable": stable, "required_child_workflow_observed": workflow_observed,
            "requests_at_stop": requests_at_stop, "generation_requests": len(requests),
            "seed": seed, "temperature": temperature, "seconds": time.monotonic() - started}
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
    parser.add_argument("--seeds", nargs="+", type=int, default=(41,))
    parser.add_argument("--temperature", type=float, default=0.3)
    parser.add_argument("--cases", nargs="+", choices=("read", "write", "replace", "cancel", "search_literal", "search_quoted", "search_regex", "search_empty", "search_context", "search_cancel"), default=("read", "write"))
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=False)
    (args.output / "production.patch").write_bytes(subprocess.check_output(["git", "diff", "--binary", "--", "codey"]))
    (args.output / "source-manifest.json").write_text(json.dumps({
        path.as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(Path("codey").rglob("*")) if path.suffix in {".py", ".mjs"}
    }, indent=2) + "\n", encoding="utf-8")
    with zipfile.ZipFile(args.output / "production-source.zip", "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for path in sorted(Path("codey").rglob("*")):
            if path.suffix in {".py", ".mjs"}:
                archive.write(path, path.as_posix())
    rows = []
    for seed in args.seeds:
        for protocol in args.protocols:
            for case in args.cases:
                directory = args.output / f"seed-{seed}" / f"{protocol}-{case}"
                try:
                    row = probe(directory, base_url=args.base_url, model=args.model, protocol=protocol,
                                case=case, seed=seed, temperature=args.temperature)
                except Exception as exc:
                    row = {"protocol": protocol, "case": case, "seed": seed, "ok": False, "error": repr(exc)}
                rows.append(row)
                print(json.dumps(row, ensure_ascii=False), flush=True)
                (args.output / "summary.json").write_text(json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8")
    return 0 if all(row["ok"] for row in rows) else 1


if __name__ == "__main__":
    raise SystemExit(main())
