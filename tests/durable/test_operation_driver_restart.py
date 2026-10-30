"""Normal operation actions and cold restart actions share the same driver."""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

SOURCE_TEXT = "A complete extracted source with a finite ending."


def _run(root: Path, boundary: str = "", resume: bool = False, source_offset: int | None = None):
    import os

    from codey.durable import DurableSession, SQLiteDurableStore
    from codey.durable.checkpoint import operation_from_checkpoint
    from codey.operations.generation_boundary import durable_generation_boundary_callback
    from codey.operations.task_effects import KernelEffectSink
    from codey.operations.task_session import TaskSession
    from codey.policies.task_policy import TaskPolicy
    from codey.providers.base import AssistantTurn, ProviderToolCall
    from codey.runtime.core.models import ToolResult
    from codey.runtime.write.mutation_line import RuntimeMutationLine
    from codey.storage.managed_outputs import ManagedOutputStore
    from tests.support.kernel_runtime import (
        KernelExecutionDeps,
        KernelObservationDeps,
        KernelRunRequest,
        KernelTransportDeps,
        run_task_kernel,
    )

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

    research_store = research_tools = None
    source_url = "https://93.184.216.34/source?pin=" + "x" * 9000
    if source_offset is not None:
        from codey.knowledge.store import KnowledgeStore
        from codey.policies.action import MAX_MANAGED_OUTPUTS_PER_RUN
        from codey.research.tools import ResearchTools

        class Search:
            def fetch(self, url):
                append({"event": "fetch"})
                return {"status": "ok", "url": url, "title": "Finite source", "text": SOURCE_TEXT, "truncated": False}

        research_store = KnowledgeStore(root / "knowledge")
        research_tools = ResearchTools(Search(), research_store, None, session_id="s")
        if not resume:
            for index in range(MAX_MANAGED_OUTPUTS_PER_RUN):
                assert sink.managed_outputs.write_tool_output(session_id="s", run_id="r", tool_id=str(index),
                    permission_profile="coding_writer", tool_name="run", display_ref="test", text=str(index)) is not None

    class Provider:
        native_tools = True

        def send_turn(self, prompt, tools):
            append({"event": "request"})
            if source_offset is not None:
                return AssistantTurn(tool_calls=(ProviderToolCall("read-original", "open_url",
                    {"url": source_url, "offset": source_offset}),))
            return AssistantTurn(tool_calls=(ProviderToolCall("read-original", "read_file", {"path": "a.py"}),))

        def send_tool_results(self, results, tools):
            append({"event": "delivery", "ids": [row.call_id for row in results], "contents": [row.content for row in results]})
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
        session = TaskSession(TaskPolicy(frozenset({"control", "project.read", "web.read"})), max_turns=4)
        result = run_task_kernel(session,
            request=KernelRunRequest(transport=KernelTransportDeps(
                provider=Provider(), run_id="r", provider_id="local"),
                execution=KernelExecutionDeps(executors={"read_file": execute}, research_tools=research_tools,
                    managed_outputs=sink.managed_outputs, session_id="s"),
                observation=KernelObservationDeps(intent_sink=sink, on_boundary=durable_generation_boundary_callback(
                    durable=durable, session_id="s", task_id="r", sink=sink))))
        state = operation_from_checkpoint(durable.task("r"), "s")
        if research_tools is not None:
            assert not session.opened_sources
            assert not research_tools.ledger.opened_sources and not research_tools.ledger.read_passages
        return result, state
    finally:
        if research_store is not None:
            research_store.close()
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


@pytest.mark.parametrize("extra_offset", [0, 8000])
def test_research_eof_error_is_persisted_restored_and_delivered_after_attachment_quota(tmp_path, extra_offset):
    from codey.durable import DurableSession, SQLiteDurableStore
    from codey.operations.kernel_receipts import restore_result_receipt
    from codey.runtime.effects.effect_records import RuntimeEffectStore
    from codey.storage.managed_outputs import ManagedOutputStore

    offset = len(SOURCE_TEXT) + extra_offset
    source = ("from pathlib import Path; from tests.durable.test_operation_driver_restart import _run; "
              "import sys; _run(Path(sys.argv[1]), 'receipt', source_offset=int(sys.argv[2]))")
    child = subprocess.run([sys.executable, "-c", source, str(tmp_path), str(offset)], capture_output=True, timeout=45)
    assert child.returncode == 73, child.stderr.decode(errors="replace")
    durable = DurableSession(SQLiteDurableStore(tmp_path / "state.sqlite"))
    try:
        projection = next(row for row in RuntimeEffectStore(durable=durable).load_effects("s", "r")
                          if row.intent.tool_name == "open_url")
        assert projection.settlement.status != "ok"
        # The long URL forces the mandatory failure receipt through JSON storage
        # after the optional attachment quota has already been exhausted.
        assert projection.settlement.result_ref.startswith("artifact_")
        original = restore_result_receipt(projection, store=ManagedOutputStore(tmp_path), session_id="s", run_id="r")
        assert original.call.name == "open_url" and original.call.args["offset"] == offset
        assert not original.ok and "No passage was read" in original.model_text
        assert "at or beyond the end" in original.model_text
        assert not original.canonical.get("research_observation") and not original.canonical.get("read_passages")
    finally:
        durable.close()
    first, state = _run(tmp_path, resume=True, source_offset=offset)
    assert first.completed, first
    before = (tmp_path / "observations.jsonl").read_bytes()
    assert _run(tmp_path, resume=True, source_offset=offset) == (first, state)
    assert (tmp_path / "observations.jsonl").read_bytes() == before
    observations = [json.loads(line) for line in before.splitlines()]
    assert sum(row["event"] == "request" for row in observations) == 1
    assert sum(row["event"] == "fetch" for row in observations) == 1
    delivered = [row for row in observations if row["event"] == "delivery"]
    assert len(delivered) == 1 and delivered[0]["ids"] == ["read-original"]
    assert original.model_text in delivered[0]["contents"][0]
