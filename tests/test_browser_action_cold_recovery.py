"""A process exit after progress commit must not recount the delivery batch."""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

from codey.durable import DurableSession, SQLiteDurableStore
from codey.durable.checkpoint import operation_from_checkpoint
from codey.durable.model import CommitResult, DurableMutation
from codey.operations.generation_boundary import durable_generation_boundary_callback
from codey.operations.kernel_contracts import (
    KernelExecutionDeps,
    KernelObservationDeps,
    KernelRunRequest,
    KernelTransportDeps,
)
from codey.operations.provider_session import ProviderAdapter
from codey.operations.task_effects import KernelEffectSink
from codey.operations.task_loop import run_task_kernel
from codey.operations.task_session import TaskSession
from codey.policies.task_policy import TaskPolicy
from codey.policies.tool_requirement import require_structured_actions, require_tool_success
from codey.runtime.core.models import ToolCall, ToolResult
from codey.runtime.core.operation_payload import RuntimeOperationState
from codey.runtime.write.mutation_line import RuntimeMutationLine
from codey.storage.managed_outputs import ManagedOutputStore
from codey.types.json_value import JsonObject, parse_json_object
from tests.test_browser_action_progress import control_snapshot


def cold_probe(root: Path, *, interrupt: bool) -> JsonObject:
    root.mkdir(parents=True, exist_ok=True)
    durable = DurableSession(SQLiteDurableStore(root / "state.sqlite"))
    line = RuntimeMutationLine(durable=durable)
    if interrupt:
        durable.admit_input(conversation_id="s", request_id="input", task_id="r", text="Observe and wait", provider_snapshot={})
        durable.claim_task("r")
        durable.start_task("r", state=dict(durable.task("r").state))
        line.accept_operation(session_id="s", run_id="r", provider_id="fixture", turn_budget=6,
                              max_repair_rounds=1, task_kind="chat")
        line.mark_writer_running("s", "r", provider_id="fixture")
    else:
        durable.recover_scheduler_state()
        if durable.task("r").status == "pending":
            durable.claim_task("r")
    outputs = ManagedOutputStore(root)
    sink = KernelEffectSink(line, session_id="s", run_id="r", provider_id="fixture", durable=durable, managed_outputs=outputs)

    def state() -> RuntimeOperationState:
        position = operation_from_checkpoint(durable.task("r"), "s")
        assert position is not None
        return position

    def log(row: JsonObject) -> None:
        with (root / "probe.jsonl").open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(row) + "\n")

    def execute(call: ToolCall) -> ToolResult:
        log({"executed": call.name})
        if call.name == "browser_snapshot":
            result = control_snapshot(f"g{state().turn}")
            return ToolResult(call, result.model_text, ok=True, canonical=result.canonical)
        assert call.name == "browser_wait"
        return ToolResult(call, "ready", ok=True,
            canonical={"value": {"action": "wait", "condition": "text", "matched": True,
                                 "page_url": "https://example.com", "text": "ready"}})

    class Provider:
        def send(self, prompt: str) -> str:
            position = state()
            log({"request": position.logical_turn, "idle": position.counters.progress.idle, "prompt": prompt})
            if position.logical_turn <= 2:
                args: JsonObject = {"action": {"kind": "snapshot"}}
            elif position.logical_turn == 3:
                assert position.counters.progress.idle == 1
                assert '"kind": "wait"' in prompt and "call tools.browser_wait" not in prompt
                args = {"action": {"kind": "wait", "condition": "text", "value": "ready", "timeout_ms": 100}}
            else:
                return json.dumps({"tool": "done", "args": {"summary": "observed and waited"}})
            return json.dumps({"tool": "codemode", "args": args})

    def committed(_result: CommitResult, _mutation: DurableMutation) -> None:
        position = state()
        progress = position.counters.progress
        if (position.leaf == "tool_delivery_pending" and position.turn == 2
                and len(progress.attempts) == 2 and progress.idle == 1):
            log({"interrupted_batch": progress.last_batch_id, "idle": progress.idle})
            os._exit(74)

    if interrupt:
        durable.subscribe_commits(committed)
    session = TaskSession(TaskPolicy(frozenset({"control", "browser.observe"}), required_checks=(
        require_structured_actions(), require_tool_success("browser_wait", via="codemode",
            arguments={"condition": "text", "value": "ready", "timeout_ms": 100}))), task_kind="chat", max_turns=6)
    request = KernelRunRequest(transport=KernelTransportDeps(ProviderAdapter(Provider()), run_id="r", provider_id="fixture"),
        execution=KernelExecutionDeps(intent_sink=sink, executors={"browser_snapshot": execute, "browser_wait": execute}),
        observation=KernelObservationDeps(on_boundary=durable_generation_boundary_callback(
            durable=durable, session_id="s", task_id="r", sink=sink)))
    try:
        result = run_task_kernel(session, request=request)
        return {"completed": result.completed, "summary": result.summary, "stop_reason": result.stop_reason}
    finally:
        durable.close()


def test_cold_progress_redelivery_uses_original_snapshot_and_renders_next_frozen_actions(tmp_path: Path) -> None:
    source = ("from pathlib import Path; import sys; "
              "from tests.test_browser_action_cold_recovery import cold_probe; "
              "cold_probe(Path(sys.argv[1]), interrupt=True)")
    child = subprocess.run([sys.executable, "-c", source, str(tmp_path)], capture_output=True, text=True, timeout=60)
    assert child.returncode == 74, child.stdout + child.stderr
    result = cold_probe(tmp_path, interrupt=False)
    assert result["completed"] is True, result
    rows = [parse_json_object(row) for row in (tmp_path / "probe.jsonl").read_text(encoding="utf-8").splitlines()]
    assert sum(row.get("executed") == "browser_snapshot" for row in rows) == 2
    assert sum(row.get("executed") == "browser_wait" for row in rows) == 1
    assert [row["idle"] for row in rows if row.get("request") == 3] == [1]
