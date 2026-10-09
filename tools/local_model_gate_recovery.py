"""Gate-only interruption at a durable settlement, followed by the formal task entry.

Two owned child processes share an isolated project and production state stores.
The observer records calls; it neither executes tools nor implements a model loop.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
import time
from dataclasses import asdict
from pathlib import Path
from unittest.mock import patch

from codey.app.headless_runner import HeadlessAppContext
from codey.operations.project_adapter import run as agent_run
from codey.operations.recovery import rebuilt_policy_from_log
from codey.operations.task_effects import KernelEffectSink
from codey.operations.task_entry import run_task_submission
from codey.operations.task_execution import ExecutionDelegate
from codey.operations.task_run import TaskRunDeps
from codey.providers.api_chat import encode_tools
from codey.providers.api_provider import ApiProvider
from codey.runtime.core.cancellation import start_process, wait_process
from codey.runtime.effects.effect_records import RuntimeEffectStore
from codey.runtime.effects.tool_result_delivery import ToolResultDeliveryStore
from codey.task.model import TaskSubmission
from codey.workspace.changes import collect_changes, is_git_repository
from tools.local_model_gate_attempts import REPO_ROOT, GateTarget, make_provider, record_api_generations, write_json

SESSION_ID = "gate-recovery-session"
RUN_ID = "gate-recovery-run"
INTERRUPTED_EXIT = 75
CONTENT = "VALUE = 42\n"
TASK = (
    "Create checkpoint.py containing exactly VALUE = 42 followed by a newline. "
    "Keep the existing tests unchanged. Run python -m unittest discover to verify the change, then finish with done."
)


def _append(path: Path, row: dict) -> None:
    with path.open("a", encoding="utf-8") as stream:
        stream.write(json.dumps(row, ensure_ascii=False) + "\n")


class _ScriptedLocalProvider(ApiProvider):
    """Deterministic HTTP boundary; all history/codec behavior remains production."""

    def __init__(self, target: GateTarget, directory: Path, stage: str):
        super().__init__(target.base_url, target.model)
        self.directory, self.stage = directory, stage
        self.responses = 0

    def _generate(self, messages, tools=None, *, timeout=None, checkpoints=None):
        self.responses += 1
        _append(self.directory / "scripted-requests.jsonl", {"messages": messages, "tools": encode_tools(tools)})
        if tools == []:
            return {"choices": [{"message": {"role": "assistant", "content": "OK"}, "finish_reason": "stop"}]}
        if self.stage == "prepare":
            name, args = "edit", {"path": "checkpoint.py", "content": CONTENT}
        elif self.responses == 1:
            name, args = "run", {"path": ".", "command": "python -m unittest discover"}
        else:
            name, args = "done", {"summary": "Created checkpoint.py and verified the existing test."}
        if tools:
            message = {"role": "assistant", "content": "", "tool_calls": [{
                "id": f"{self.stage}-{self.responses}", "type": "function",
                "function": {"name": name, "arguments": json.dumps(args)},
            }]}
        else:
            message = {"role": "assistant", "content": json.dumps({"tool": name, "args": args})}
        return {"choices": [{"message": message, "finish_reason": "tool_calls" if tools else "stop"}]}


def _stage(directory: Path, stage: str, *, scripted: bool) -> int:
    config = json.loads((directory / "recovery-input.json").read_text(encoding="utf-8"))
    target = GateTarget(**config["target"])
    project, state_home = Path(config["project"]), Path(config["state"])
    from codey.providers import local_config

    local_config.DEFAULT_STATE_HOME = state_home
    local_config.save_local_config(local_config.LocalProviderConfig(
        base_url=target.base_url, model=target.model, connection_revision="isolated-gate-target",
        native_tools_mode="on" if target.protocol == "native" else "off",
        api_protocol=target.api_protocol,
        context=local_config.LocalContextBudget(target.context_window_tokens, target.context_reserve_tokens, target.context_keep_recent_tokens),
    ))
    os.environ["NATIVE_TOOLS"] = "1" if target.protocol == "native" else "0"
    provider = (_ScriptedLocalProvider(target, directory, stage) if scripted else make_provider(target, directory))
    def emit(row):
        _append(directory / f"{stage}-events.jsonl", row)

    context = HeadlessAppContext(state_home, port=0, emit_jsonl=emit, connect_provider=lambda *a, **k: provider)
    deps = TaskRunDeps.from_submission_stores(
        state=context, stores=context.task_submission_stores, agent_run=agent_run,
        collect_changes=collect_changes, run_review=lambda **kwargs: None,
        capture_provider_failure=lambda *a, **k: None, is_git_repository=is_git_repository,
        review_fix_turns=0, review_log_lines=0,
    )
    execute = ExecutionDelegate._execute_project
    settle = KernelEffectSink.settle

    def observed_execute(self, call):
        _append(directory / "executions.jsonl", {"stage": stage, "tool": call.name, "pid": os.getpid()})
        return execute(self, call)

    def interrupted_settle(self, identity, ok, *, result=None, exit_code=None):
        settle(self, identity, ok, result=result, exit_code=exit_code)
        if stage == "prepare" and result is not None and result.call.name == "edit" and ok is True:
            policy = rebuilt_policy_from_log(context.runtime_mutations.session_log, session_id=SESSION_ID, run_id=RUN_ID)
            write_json(directory / "interruption.json", {
                "pid": os.getpid(), "effect_id": identity, "call_id": result.call.call_id,
                "result_text": result.model_text, "policy": policy.to_payload(),
                "file_sha256": hashlib.sha256((project / "checkpoint.py").read_bytes()).hexdigest(),
            })
            # Deliberate process death after settlement, before result delivery.
            # No finally/close is run: the second process must recover from disk.
            os._exit(INTERRUPTED_EXIT)

    try:
        with patch.object(ExecutionDelegate, "_execute_project", observed_execute), \
             patch.object(KernelEffectSink, "settle", interrupted_settle):
            run_task_submission(deps, TaskSubmission(
                session_id=SESSION_ID, run_id=RUN_ID, project=str(project), task=TASK,
                max_turns=target.turn_budget or 8, provider_id=target.provider_id, intent="project", continue_task=(stage == "resume"),
                model_selection=(target.api_selection if target.provider_id != "local" else {}),
                project_changes_required=True,
            ))
        terminal = context.run_registry.last_terminal_event() or {}
        policy = rebuilt_policy_from_log(context.runtime_mutations.session_log, session_id=SESSION_ID, run_id=RUN_ID)
        effects = RuntimeEffectStore(context.runtime_mutations.session_log).load_effects(SESSION_ID, RUN_ID)
        original = json.loads((directory / "interruption.json").read_text(encoding="utf-8")) if stage == "resume" else {}
        recovered = next((effect for effect in effects if effect.intent.effect_id == original.get("effect_id")), None)
        batches = ToolResultDeliveryStore(context.runtime_mutations.session_log).load_batches(SESSION_ID, RUN_ID)
        original_delivered = any(batch.is_delivered and original.get("effect_id") in batch.intent.tool_refs
                                 for batch in batches)
        write_json(directory / f"{stage}-result.json", {
            "pid": os.getpid(), "stop_reason": terminal.get("stop_reason"), "terminal": terminal,
            "policy": policy.to_payload(), "original_result_delivered": original_delivered,
            "original_call_id_preserved": recovered is not None and recovered.intent.call_id == original.get("call_id"),
        })
        return 0
    finally:
        provider.close()
        if not context.close():
            raise RuntimeError("recovery stage context did not close")


def _verify_recovery(directory: Path, project: Path, prepare_exit: int) -> dict:
    original = json.loads((directory / "interruption.json").read_text(encoding="utf-8"))
    resumed = json.loads((directory / "resume-result.json").read_text(encoding="utf-8"))
    executions = [json.loads(line) for line in (directory / "executions.jsonl").read_text(encoding="utf-8").splitlines()]
    edit_executions = sum(row["tool"] == "edit" for row in executions)
    unchanged = hashlib.sha256((project / "checkpoint.py").read_bytes()).hexdigest() == original["file_sha256"]
    artifact_correct = (project / "checkpoint.py").read_text(encoding="utf-8") == CONTENT
    completed = subprocess.run([sys.executable, "-m", "unittest", "discover"], cwd=project,
                               capture_output=True, text=True, encoding="utf-8", timeout=30)
    verification = {"ok": completed.returncode == 0 and "Ran 1 test" in completed.stderr,
                    "exit_code": completed.returncode, "output": completed.stdout + completed.stderr}
    requests_path = directory / "provider.jsonl"
    if not requests_path.exists():
        requests_path = directory / "scripted-requests.jsonl"
    records = [json.loads(line) for line in requests_path.read_text(encoding="utf-8").splitlines()]
    requests = [row.get("payload", row) for row in records if row.get("type", "request") == "request"]
    # A new provider window receives recovered facts as text. Old call ids
    # remain in the receipts; they must not become orphan native tool results.
    delivered_text = any(original["result_text"] in json.dumps(row.get("messages", row.get("input", [])), ensure_ascii=False)
                         for row in requests)
    policy_preserved = resumed["policy"] == original["policy"]
    checks = (prepare_exit == INTERRUPTED_EXIT, original["pid"] != resumed["pid"], edit_executions == 1,
              unchanged, artifact_correct, verification["ok"], policy_preserved, resumed["original_result_delivered"], delivered_text,
              resumed["original_call_id_preserved"], resumed["stop_reason"] == "done")
    return {
        "case": "recovery", "ok": all(checks), "prepare_exit_code": prepare_exit,
        "prepare_pid": original["pid"], "resume_pid": resumed["pid"], "edit_executions": edit_executions,
        "original_result_delivered": resumed["original_result_delivered"] and delivered_text,
        "original_call_id_preserved": resumed["original_call_id_preserved"],
        "delivery_mode": "fresh_window_fact_handoff", "policy_preserved": policy_preserved,
        "artifact_correct": artifact_correct,
        "stop_reason": resumed["stop_reason"], "verification": verification,
        "failure_stage": "" if all(checks) else "recovery_contract",
    }


def run_recovery_case(target: GateTarget, directory: Path, *, scripted: bool = False) -> dict:
    config = json.loads((directory / "input.json").read_text(encoding="utf-8"))
    project, state_home = Path(config["project"]), Path(config["state"])
    project.mkdir(parents=True, exist_ok=True)
    tests = project / "tests"
    tests.mkdir()
    (tests / "__init__.py").write_text("", encoding="utf-8")
    test_path = tests / "test_checkpoint.py"
    test_text = "import unittest\nfrom checkpoint import VALUE\nclass TestCheckpoint(unittest.TestCase):\n    def test_value(self):\n        self.assertEqual(VALUE, 42)\n"
    test_path.write_text(test_text, encoding="utf-8")
    write_json(directory / "recovery-input.json", {"target": asdict(target), "project": str(project), "state": str(state_home)})
    deadline = time.monotonic() + target.request_timeout
    exits = []
    for stage in ("prepare", "resume"):
        command = [sys.executable, "-B", "-m", "tools.local_model_gate_recovery", "--stage", stage,
                   "--directory", str(directory)]
        if scripted:
            command.append("--scripted")
        process, job = start_process(command, cwd=REPO_ROOT)
        completed = wait_process(process, job, command, max(0.001, deadline - time.monotonic()), capture_limit_bytes=256_000)
        (directory / f"{stage}-worker.log").write_text(completed.stdout + completed.stderr, encoding="utf-8")
        exits.append(completed.returncode)
        expected = INTERRUPTED_EXIT if stage == "prepare" else 0
        if completed.returncode != expected:
            return {"case": "recovery", "ok": False, "failure_stage": f"recovery_{stage}",
                    "error": f"expected exit {expected}, got {completed.returncode}; see {stage}-worker.log"}
    result = _verify_recovery(directory, project, exits[0])
    if test_path.read_text(encoding="utf-8") != test_text:
        result.update(ok=False, failure_stage="recovery_test_modified")
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--stage", choices=("prepare", "resume"), required=True)
    parser.add_argument("--directory", type=Path, required=True)
    parser.add_argument("--scripted", action="store_true")
    args = parser.parse_args()
    config = json.loads((args.directory / "recovery-input.json").read_text(encoding="utf8"))
    if not args.scripted and config["target"].get("provider_id", "local") != "local":
        with record_api_generations(args.directory / "provider.jsonl"):
            return _stage(args.directory, args.stage, scripted=False)
    return _stage(args.directory, args.stage, scripted=args.scripted)


if __name__ == "__main__":
    raise SystemExit(main())
