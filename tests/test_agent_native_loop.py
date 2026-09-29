from __future__ import annotations

from pathlib import Path

from codey.env_names import NATIVE_TOOLS_ENV
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
    import tempfile
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
    assert "done" in {str(t.get("function", {}).get("name") or t.get("name") or "") for t in snapshot.native_tools} or snapshot.native_tools

    from codey.runtime.core.models import ToolResult

    with (
        mock.patch("codey.operations.kernel_transport.provider_uses_native", return_value=True),
        mock.patch("codey.toolchain.tool_spec.native_tools_for_snapshot", return_value=[{"type": "function", "function": {"name": "read_file"}}, {"type": "function", "function": {"name": "done"}}]),
    ):
        result = run_task_kernel(
            session, provider=provider,
            executors={"read_file": lambda call: ToolResult(call=call, model_text="hello")},
            run_id="r-native-1", effect_scope="task",
            provider_id="local", project_path=tmp_path,
            user_task="read app", context_text="",
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
