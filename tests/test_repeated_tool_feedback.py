"""A repeated successful plan gets feedback before the bounded loop stops."""
import json

import pytest

from codey.operations.kernel_contracts import KernelExecutionDeps, KernelRunRequest, KernelTransportDeps
from codey.operations.task_session import TaskSession
from codey.policies.task_policy import TaskPolicy
from codey.providers.base import AssistantTurn, ProviderToolCall
from codey.runtime.core.models import ToolResult
from tests.support.kernel_runtime import run_task_kernel


@pytest.mark.parametrize("native", [False, True])
@pytest.mark.parametrize("obeys_feedback", [False, True])
def test_repeat_feedback_is_delivered_without_disabling_loop_guard(monkeypatch, native, obeys_feedback):
    monkeypatch.setattr("codey.operations.kernel_transport.provider_uses_native", lambda *a, **k: native)
    feedback, executed = [], []

    class Provider:
        turns = 0

        def reply(self, prompt=""):
            feedback.append(prompt)
            self.turns += 1
            corrected = obeys_feedback and "already succeeded" in prompt
            name = "done" if corrected else "research_plan"
            args = {"summary": "Plan recorded; further investigation is needed."} if corrected else {"questions": ["Which endpoints exist?"]}
            return (AssistantTurn(reasoning="Register the questions first.", tool_calls=(
                ProviderToolCall(f"call-{self.turns}", name, args),)) if native else json.dumps({"tool": name, "args": args}))

        def send(self, prompt):
            return self.reply(prompt)

        def send_turn(self, prompt, tools):
            return self.reply(prompt)

        def send_tool_results(self, messages, tools):
            return self.reply("\n".join(row.user_content for row in messages))

        def acknowledge_tool_results(self, results, declared_tools, timeout=None):
            return AssistantTurn()

    def execute(call):
        executed.append(call.name)
        return ToolResult(call, 'The plan is recorded. Investigate the questions.', ok=True,
                          canonical={"research_observation": {"questions": call.args["questions"]}})

    session = TaskSession(TaskPolicy(frozenset({"knowledge.read", "control"})), max_turns=8)
    result = run_task_kernel(session, request=KernelRunRequest(
        transport=KernelTransportDeps(Provider(), user_task="Record a plan and explain the remaining work."),
        execution=KernelExecutionDeps(executors={"research_plan": execute})))
    assert any("already succeeded" in text for text in feedback), feedback
    assert result.stop_reason == ("done" if obeys_feedback else "no_progress"), result.summary
    assert executed == ["research_plan"] * (2 if obeys_feedback else 3)


@pytest.mark.parametrize("ok", [False, True])
def test_repeat_feedback_survives_progress_restore_but_new_results_clear_it(ok):
    from codey.operations.kernel_progress import KernelProgress
    from codey.runtime.core.models import ToolCall

    session = TaskSession(TaskPolicy(frozenset({"control"})))
    call = ToolCall("read_file", {"path": "app.py"})
    progress = KernelProgress()
    result = ToolResult(call, "unchanged output", ok=ok)
    assert not progress.observe([result], session, batch_id="one")
    assert progress.repeat_feedback() == ""
    assert not progress.observe([result], session, batch_id="two")
    restored = KernelProgress(snapshot=progress.snapshot())
    assert restored.repeat_feedback() == progress.repeat_feedback()
    assert ("already succeeded" if ok else "already failed") in restored.repeat_feedback()
    novel = ToolResult(call, "new information", ok=True)
    assert not restored.observe([novel], session, batch_id="three")
    assert restored.repeat_feedback() == ""
