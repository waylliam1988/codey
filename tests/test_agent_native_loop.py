from __future__ import annotations

from pathlib import Path

from codey.env_names import NATIVE_TOOLS_ENV
from codey.operations.task_loop import KernelExecutionDeps, KernelRunRequest, KernelTransportDeps
from codey.providers.base import AssistantTurn, ProviderToolCall
from codey.runtime.core.models import ToolCall


class FakeStructuredProvider:
    name = "local"

    def __init__(self, turns: list[AssistantTurn]) -> None:
        self._turns = list(turns)
        self.tool_payloads: list[object] = []
        self.sent_prompts: list[str] = []

    def new_chat(self, timeout=None) -> None:
        return None

    def send(self, text: str, timeout=None) -> str:
        raise AssertionError("native loop must not use text send()")

    def send_turn(self, prompt: str, tools=None, timeout=None) -> AssistantTurn:
        self.sent_prompts.append(prompt)
        self.tool_payloads.append(tools)
        return self._turns.pop(0)

    def send_tool_results(self, results, tools=None, timeout=None) -> AssistantTurn:
        self.tool_payloads.append(tools)
        assert results and results[0]["tool_call_id"]
        assert results[0]["role"] == "tool"
        return self._turns.pop(0)

    def close(self) -> None:
        return None


def _policy():
    from types import SimpleNamespace

    return SimpleNamespace(allows=lambda g: True)


def test_native_loop_read_then_done(monkeypatch, tmp_path: Path) -> None:
    """Production native loop: read_file then done via run_task_kernel."""
    from unittest import mock

    monkeypatch.setenv(NATIVE_TOOLS_ENV, "1")
    (tmp_path / "app.py").write_text("hello\n", encoding="utf-8")
    provider = FakeStructuredProvider([
        AssistantTurn(text="", tool_calls=(ProviderToolCall(id="call_1", name="read_file", arguments={"path": "app.py"}),)),
        AssistantTurn(text="", tool_calls=(ProviderToolCall(id="call_2", name="done", arguments={"summary": "ok"}),)),
        AssistantTurn(text="", tool_calls=()),
    ])
    from codey.operations.kernel_protocol import build_turn_snapshot
    from codey.operations.task_loop import run_task_kernel
    from codey.operations.task_session import TaskSession
    from codey.policies.task_policy import TaskPolicy

    policy = TaskPolicy(grants=frozenset({"project.read", "control"}))
    session = TaskSession(policy=policy, task_kind="project", project=str(tmp_path), max_turns=5)
    snapshot = build_turn_snapshot(session, native=True)
    assert snapshot.native_tools
    names = {str(t.get("function", {}).get("name") or t.get("name") or "") for t in snapshot.native_tools}
    assert "read_file" in names, f"real snapshot must contain read_file: {names}"
    assert "done" in names, f"real snapshot must contain done: {names}"

    from codey.runtime.core.models import ToolResult

    with mock.patch("codey.operations.kernel_transport.provider_uses_native", return_value=True):
        result = run_task_kernel(
            session,
            request=KernelRunRequest(
                transport=KernelTransportDeps(
                    provider=provider,
                    run_id="r-native-1",
                    effect_scope="task",
                    provider_id="local",
                    user_task="read app",
                    context_text="",
                ),
                execution=KernelExecutionDeps(
                    executors={"read_file": lambda call: ToolResult(call=call, model_text="hello")},
                    project_path=tmp_path,
                ),
            ),
        )
    assert result.stop_reason == "done"
    assert result.summary == "ok"


def test_native_call_id_flows_to_tool_result(tmp_path: Path) -> None:
    """Production protocol preserves native call ids."""
    from codey.operations.kernel_protocol import normalize_turn

    plan = normalize_turn(
        AssistantTurn(text="", tool_calls=(ProviderToolCall(id="call_9", name="read_file", arguments={"path": "x"}),)),
        policy=_policy(),
    )
    assert isinstance(plan.calls[0], ToolCall)
    assert plan.calls[0].call_id == "call_9"
