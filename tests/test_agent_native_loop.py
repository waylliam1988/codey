from __future__ import annotations

from pathlib import Path

from codey.agents.loop import _run_loop, _setup_loop
from codey.agents.request import AgentRequest
from codey.agents.state import AgentLoopSession
from codey.providers.base import AssistantTurn, ProviderToolCall
from codey.runtime.core.models import ToolCall
from codey.toolchain.runtime import ToolOutcome


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


def _request(provider: FakeStructuredProvider, tmp_path: Path) -> AgentRequest:
    from codey.agents.tools import AgentToolFns

    def read_file(root: Path, rel: str, **kwargs: object) -> ToolOutcome:
        assert rel == "app.py"
        return ToolOutcome("hello", True)

    tool_fns = AgentToolFns(read_file=read_file)  # type: ignore[arg-type]
    return AgentRequest(
        provider=provider,  # type: ignore[arg-type]
        project=tmp_path,
        task="read app",
        on_event=lambda event: None,
        tool_fns=tool_fns,
        provider_id="local",
        max_turns=5,
    )


def test_native_loop_read_then_done(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("CODEY_NATIVE_TOOLS", "1")
    provider = FakeStructuredProvider([
        AssistantTurn(text="", tool_calls=(ProviderToolCall(id="call_1", name="read", arguments={"path": "app.py"}),)),
        AssistantTurn(text="", tool_calls=(ProviderToolCall(id="call_2", name="done", arguments={"summary": "ok"}),)),
    ])
    session: AgentLoopSession = _setup_loop(_request(provider, tmp_path))
    assert session.native_tools is not None
    assert all(t["function"]["name"] != "parallel" for t in session.native_tools)
    from codey.agents.prompt_context import initial_structured_reply

    result = _run_loop(session, initial_structured_reply(session), start_turn=1)
    assert result.stop_reason == "done"
    assert result.summary == "ok"


def test_native_call_id_flows_to_tool_result(tmp_path: Path) -> None:
    from codey.protocols.native_openai import NativeOpenAIToolCodec

    codec = NativeOpenAIToolCodec()
    plan = codec.parse_turn(
        AssistantTurn(text="", tool_calls=(ProviderToolCall(id="call_9", name="read", arguments={"path": "x"}),))
    )
    assert isinstance(plan.calls[0], ToolCall)
    assert plan.calls[0].call_id == "call_9"
