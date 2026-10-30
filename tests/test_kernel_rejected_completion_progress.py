"""Rejected reports share the durable kernel progress budget, in both protocols."""

import json

import pytest

from codey.operations.completion_gate import GateVerdict
from codey.operations.kernel_contracts import KernelRunRequest, KernelTransportDeps
from codey.operations.task_session import TaskSession
from codey.policies.task_policy import TaskPolicy
from codey.providers.base import AssistantTurn, ProviderToolCall
from tests.support.kernel_runtime import run_task_kernel


@pytest.mark.parametrize("native", [False, True])
@pytest.mark.parametrize("changing", [False, True])
def test_repeated_rejected_completion_stops_but_real_revisions_get_another_attempt(monkeypatch, native, changing):
    monkeypatch.setattr("codey.operations.kernel_transport.provider_uses_native", lambda *a, **k: native)
    monkeypatch.setattr("codey.operations.completion_gate.evaluate", lambda *a, **k: GateVerdict(
        complete=False, followup="report_sections.counter needs a citation", proof=None))

    class Provider:
        def __init__(self):
            self.turns = 0
            self.closed = []

        def reply(self):
            self.turns += 1
            args = {"summary": f"draft-{self.turns}" if changing else "same rejected draft"}
            return (AssistantTurn(tool_calls=(ProviderToolCall(
                id=f"done-{self.turns}", name="done", arguments=args),)) if native
                else json.dumps({"tool": "done", "args": args}))

        def send(self, prompt):
            return self.reply()

        def send_turn(self, prompt, tools):
            return self.reply()

        def send_tool_results(self, messages, tools):
            return self.reply()

        def acknowledge_tool_results(self, results, declared_tools, timeout=None):
            self.closed.extend(row.call_id for row in results)
            return AssistantTurn()

    provider = Provider()
    session = TaskSession(policy=TaskPolicy(frozenset({"control"})), max_turns=9)
    result = run_task_kernel(session, request=KernelRunRequest(transport=KernelTransportDeps(provider)))
    assert not result.completed
    assert result.stop_reason == ("max_turns" if changing else "no_progress"), result.summary
    assert provider.turns == (9 + int(native) if changing else 3)
    if not changing:
        assert "same rejected draft" in result.summary  # Preserve partial work without accepting it.
        assert "counter needs a citation" in result.summary
    if native:
        assert f"done-{provider.turns}" in provider.closed


def test_completion_repeat_history_survives_checkpoint_roundtrip_without_fake_tool_batch():
    from codey.operations.kernel_progress import KernelProgress
    from codey.runtime.core.models import ToolCall, ToolResult
    from codey.runtime.core.progress import ProgressSnapshot

    session = TaskSession(TaskPolicy(frozenset({"control", "knowledge.read"})))
    progress = KernelProgress()
    observation = ToolResult(ToolCall("knowledge_read", {"id": "premise"}), "earlier evidence", ok=True)
    progress.observe([observation], session, batch_id="actual-settled-batch")
    for turn in (1, 2):
        session.turn = turn
        progress.reject_completion({"summary": "unchanged"}, "counter needs citation", session)
    saved = ProgressSnapshot.from_payload(json.loads(json.dumps(progress.snapshot().to_payload())))
    restored = KernelProgress(snapshot=saved)
    assert not restored.stopped
    session.turn = 3
    restored.reject_completion({"summary": "unchanged"}, "counter needs citation", session)
    assert restored.stopped
    assert restored.snapshot().last_batch_id == "actual-settled-batch"
    # A genuinely new read resets stagnation and interrupts the exact cycle.
    restored.observe([ToolResult(observation.call, "new evidence", ok=True)], session, batch_id="next-real-batch")
    assert not restored.stopped
