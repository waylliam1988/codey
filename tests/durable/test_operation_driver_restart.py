"""Normal operation actions and cold restart actions share the same driver."""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest


def _run(root: Path, boundary: str = "", resume: bool = False):
    import os

    from codey.durable import DurableSession, SQLiteDurableStore
    from codey.durable.checkpoint import operation_from_checkpoint
    from codey.operations.generation_boundary import durable_generation_boundary_callback
    from codey.operations.task_effects import KernelEffectSink, KernelRecordedProvider
    from codey.operations.task_loop import (
        KernelExecutionDeps,
        KernelObservationDeps,
        KernelRunRequest,
        KernelTransportDeps,
        run_task_kernel,
    )
    from codey.operations.task_session import TaskSession
    from codey.policies.task_policy import TaskPolicy
    from codey.providers.base import AssistantTurn, ProviderToolCall
    from codey.runtime.core.models import ToolResult
    from codey.runtime.write.mutation_line import RuntimeMutationLine
    from codey.storage.managed_outputs import ManagedOutputStore

    durable = DurableSession(SQLiteDurableStore(root / "state.sqlite"))
    if resume:
        durable.recover_scheduler_state()
        if durable.task("r").status == "pending":
            durable.claim_task("r")
    else:
        durable.admit_input(conversation_id="s", request_id="input", task_id="r", text="read a.py",
                            provider_snapshot={"provider_id": "local"})
        durable.claim_task("r")
        durable.start_task("r", state=dict(durable.task("r").state))
    line = RuntimeMutationLine(durable=durable)
    if not resume:
        line.accept_operation(session_id="s", run_id="r", provider_id="local", turn_budget=4,
                              max_repair_rounds=1, task_kind="project")
        line.mark_writer_running("s", "r", provider_id="local")
    sink = KernelEffectSink(line, session_id="s", run_id="r", provider_id="local", durable=durable,
                            managed_outputs=ManagedOutputStore(root))

    def append(row):
        with (root / "observations.jsonl").open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(row) + "\n")

    class Provider:
        native_tools = True

        def send_turn(self, prompt, tools):
            append({"event": "request"})
            return AssistantTurn(tool_calls=(ProviderToolCall("read-original", "read_file", {"path": "a.py"}),))

        def send_tool_results(self, results, tools):
            append({"event": "delivery", "ids": [row.call_id for row in results]})
            return AssistantTurn(tool_calls=(ProviderToolCall("done-original", "done", {"summary": "finished"}),))

        def acknowledge_tool_results(self, results, tools):
            append({"event": "ack", "ids": [row.call_id for row in results]})
            return AssistantTurn()

    def execute(call):
        append({"event": "execute"})
        return ToolResult(call, "original complete result", ok=True)

    if boundary:
        def committed(_result, _mutation):
            state = operation_from_checkpoint(durable.task("r"), "s")
            if ((boundary == "receipt" and state.leaf == "tool_delivery_pending" and not state.counters.progress.last_batch_id)
                    or (boundary == "intent" and state.leaf == "tool_effect_pending")
                    or (boundary == "plan" and state.leaf == "plan_ready")
                    or (boundary == "completion" and state.leaf == "control_ready" and state.work_ref)):
                os._exit(73)
        durable.subscribe_commits(committed)
    try:
        result = run_task_kernel(TaskSession(TaskPolicy(frozenset({"control", "project.read"})), max_turns=4),
            request=KernelRunRequest(transport=KernelTransportDeps(
                provider=KernelRecordedProvider(Provider(), sink), run_id="r", provider_id="local"),
                execution=KernelExecutionDeps(executors={"read_file": execute}),
                observation=KernelObservationDeps(intent_sink=sink, on_boundary=durable_generation_boundary_callback(
                    durable=durable, session_id="s", task_id="r", sink=sink))))
        state = operation_from_checkpoint(durable.task("r"), "s")
        return result, state
    finally:
        durable.close()


@pytest.mark.parametrize("boundary", ["plan", "intent", "receipt", "completion"])
def test_restart_uses_original_plan_and_reduces_progress_exactly_once(tmp_path, boundary):
    source = "from pathlib import Path; from tests.durable.test_operation_driver_restart import _run; import sys; _run(Path(sys.argv[1]), sys.argv[2])"
    child = subprocess.run([sys.executable, "-c", source, str(tmp_path), boundary], capture_output=True, timeout=45)
    assert child.returncode == 73, child.stderr.decode(errors="replace")
    first, state = _run(tmp_path, resume=True)
    assert first.completed, first
    assert state.counters.progress.last_batch_id
    assert len(state.counters.progress.attempts) == 1
    before = (tmp_path / "observations.jsonl").read_bytes()
    second, state_again = _run(tmp_path, resume=True)
    assert second == first
    assert state_again == state
    assert (tmp_path / "observations.jsonl").read_bytes() == before
    observations = [json.loads(line) for line in before.splitlines()]
    assert sum(row["event"] == "request" for row in observations) == 1
    assert sum(row["event"] == "execute" for row in observations) == 1
    assert [row["ids"] for row in observations if row["event"] == "delivery"] == [["read-original"]]
