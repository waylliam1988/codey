"""Prepared coding context must not contradict native tool transport."""

from codey.operations.task_loop import KernelRunRequest, KernelTransportDeps, run_task_kernel
from codey.operations.task_session import TaskSession
from codey.policies.task_policy import TaskPolicy
from codey.providers.base import AssistantTurn


def test_native_first_send_does_not_demand_json_from_read_file_context(monkeypatch):
    from codey.env_names import NATIVE_TOOLS_ENV

    monkeypatch.setenv(NATIVE_TOOLS_ENV, "1")
    sent = []

    class Provider:
        def send_turn(self, text, tools):
            sent.append(text)
            return AssistantTurn(text="no tool")

        def send_tool_results(self, messages, tools):
            raise AssertionError("no native tool calls were issued")

        def acknowledge_tool_results(self, results, declared_tools, timeout=None):
            return self.send_tool_results(results, [])

    session = TaskSession(policy=TaskPolicy(grants=frozenset({"control", "project.read"})),
                          read_files={"app.py"}, max_turns=1)
    run_task_kernel(session, request=KernelRunRequest(
        transport=KernelTransportDeps(provider=Provider(), provider_id="local"),
    ))
    assert len(sent) == 1
    assert "Files read this run: app.py" in sent[0]
    assert "Call exactly one native tool" in sent[0]
    assert "Reply with exactly one JSON object" not in sent[0]
