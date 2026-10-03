"""Native loop uses the real snapshot schema with read_file and done.

Repro: the old test asserted
``assert "done" in names or snapshot.native_tools`` after already
asserting the list was non-empty, so the second half made the assertion
vacuous; it then mocked the real schema generator.

Lock: the fake provider receives the real snapshot native_tools (no
schema mock), and the test asserts both read_file and done are present.
"""
from __future__ import annotations

from pathlib import Path

from codey.operations.task_loop import KernelExecutionDeps, KernelRunRequest, KernelTransportDeps


def test_native_loop_uses_real_snapshot_with_read_and_done(tmp_path: Path) -> None:
    from unittest import mock

    from codey.env_names import NATIVE_TOOLS_ENV  # noqa: F401  (kept for parity, not needed)
    from codey.operations.kernel_protocol import build_turn_snapshot
    from codey.operations.task_loop import run_task_kernel
    from codey.operations.task_session import TaskSession
    from codey.policies.task_policy import TaskPolicy
    from codey.providers.base import AssistantTurn, ProviderToolCall
    from codey.runtime.core.models import ToolResult

    (tmp_path / "app.py").write_text("hello\n", encoding="utf-8")

    class FakeStructuredProvider:
        name = "local"

        def __init__(self, turns: list[AssistantTurn]) -> None:
            self._turns = list(turns)
            self.seen_tools: list[object] = []

        def new_chat(self, timeout=None) -> None:
            return None

        def send(self, text: str, timeout=None) -> str:
            raise AssertionError("native loop must not use text send()")

        def send_turn(self, prompt: str, tools=None, timeout=None) -> AssistantTurn:
            self.seen_tools.append(tools)
            return self._turns.pop(0)

        def send_tool_results(self, results, tools=None, timeout=None) -> AssistantTurn:
            self.seen_tools.append(tools)
            assert results and results[0]["tool_call_id"]
            assert results[0]["role"] == "tool"
            return self._turns.pop(0)

        def close(self) -> None:
            return None

    provider = FakeStructuredProvider([
        AssistantTurn(text="", tool_calls=(ProviderToolCall(id="call_1", name="read_file", arguments={"path": "app.py"}),)),
        AssistantTurn(text="", tool_calls=(ProviderToolCall(id="call_2", name="done", arguments={"summary": "ok"}),)),
        AssistantTurn(text="", tool_calls=()),
    ])
    policy = TaskPolicy(grants=frozenset({"project.read", "control"}))
    session = TaskSession(policy=policy, task_kind="project", project=str(tmp_path), max_turns=5)
    snapshot = build_turn_snapshot(session, native=True)
    assert snapshot.native_tools
    names = {str(t.get("function", {}).get("name") or t.get("name") or "") for t in snapshot.native_tools}
    assert "read_file" in names, f"real snapshot must contain read_file: {names}"
    assert "done" in names, f"real snapshot must contain done: {names}"

    with mock.patch("codey.operations.kernel_transport.provider_uses_native", return_value=True):
        result = run_task_kernel(
            session,
            request=KernelRunRequest(
                transport=KernelTransportDeps(
                    provider=provider,
                    run_id="r-native-real-1",
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
    # Provider really received the real schema, not a mocked two-item list.
    assert provider.seen_tools and provider.seen_tools[0] is not None
    first = provider.seen_tools[0]
    assert isinstance(first, (list, tuple)) and len(list(first)) > 2, f"must be the real schema: {first!r:.200}"
