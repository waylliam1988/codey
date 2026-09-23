from __future__ import annotations

from pathlib import Path

from codey.agents.loop import _run_loop, _setup_loop
from codey.agents.request import AgentRequest
from codey.agents.result_delivery import deliver_turn_results
from codey.agents.state import AgentLoopSession
from codey.agents.tool_execution import TurnState, record_tool_outcome
from codey.providers.base import AssistantTurn, ProviderToolCall
from codey.providers.error_classification import ContextOverflowError
from codey.runtime.core.models import ToolCall
from codey.toolchain.runtime import ToolOutcome


class FakeMutations:
    def __init__(self) -> None:
        self.batches: list = []
        self.begins: list = []
        self.settlements: list = []

    def begin_tool_batch(self, session_id, run_id, *, intents=(), delivery_intent=None):
        self.batches.append(delivery_intent)

    def begin_provider_effect(self, session_id, run_id, intent, *, driver=None, delivery_batch_id=""):
        self.begins.append((intent, delivery_batch_id))
        return intent

    def settle_provider_effect(self, session_id, run_id, settlement):
        self.settlements.append(settlement)

    def settle_tool_effect(self, session_id, run_id, settlement):
        return None


class FakeDeliveryStore:
    def load_batches(self, session_id, run_id):
        return []


class FakeStructuredProvider:
    name = "local"

    def __init__(self, turns: list[AssistantTurn]) -> None:
        self._turns = list(turns)
        self.tool_results_seen: list = []
        self.chats_opened = 0

    def new_chat(self, timeout=None) -> None:
        self.chats_opened += 1

    def send(self, text: str, timeout=None) -> str:
        raise AssertionError("native path must not use text send()")

    def send_turn(self, prompt: str, tools=None, timeout=None) -> AssistantTurn:
        return self._turns.pop(0)

    def send_tool_results(self, results, tools=None, timeout=None) -> AssistantTurn:
        self.tool_results_seen.append(list(results))
        return self._turns.pop(0)

    def close(self) -> None:
        return None


def _request(provider, tmp_path: Path, **extra) -> AgentRequest:
    from codey.agents.tools import AgentToolFns

    def read_file(root: Path, rel: str, **kwargs: object) -> ToolOutcome:
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
        **extra,
    )


def test_native_delivery_records_effect_and_batch(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("CODEY_NATIVE_TOOLS", "1")
    mutations = FakeMutations()
    provider = FakeStructuredProvider([
        AssistantTurn(text="", tool_calls=(ProviderToolCall(id="c9", name="done", arguments={"summary": "ok"}),)),
    ])
    session: AgentLoopSession = _setup_loop(_request(
        provider, tmp_path, session_id="s", run_id="r",
        runtime_mutations=mutations, tool_result_delivery=FakeDeliveryStore(),
    ))
    assert session.native_tools is not None
    turn_state = TurnState()
    record_tool_outcome(
        session, turn_state, turn=1,
        call=ToolCall(name="read", args={"path": "app.py"}, call_id="c1"),
        outcome=ToolOutcome("hello", True), tool_index=0, ref="1:0",
    )
    reply = deliver_turn_results(session, turn_state, 1)
    assert isinstance(reply, AssistantTurn)
    assert len(mutations.batches) == 1
    assert len(mutations.begins) == 1
    intent, batch_id = mutations.begins[0]
    assert batch_id == turn_state.delivery_batch_id
    assert batch_id
    assert len(mutations.settlements) == 1
    assert getattr(mutations.settlements[0], "status", "") == "ok"
    sent = provider.tool_results_seen[0][0]
    assert sent["role"] == "tool" and sent["tool_call_id"] == "c1"


def test_native_overflow_falls_back_to_text(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("CODEY_NATIVE_TOOLS", "1")

    class OverflowProvider(FakeStructuredProvider):
        def __init__(self) -> None:
            super().__init__([AssistantTurn(text="ack")])
            self.fallback_prompts: list[str] = []

        def send_tool_results(self, results, tools=None, timeout=None) -> AssistantTurn:
            raise ContextOverflowError("full")

        def send_turn(self, prompt: str, tools=None, timeout=None) -> AssistantTurn:
            self.fallback_prompts.append(prompt)
            return self._turns.pop(0)

    provider = OverflowProvider()
    session: AgentLoopSession = _setup_loop(_request(provider, tmp_path))
    turn_state = TurnState()
    record_tool_outcome(
        session, turn_state, turn=1,
        call=ToolCall(name="read", args={"path": "app.py"}, call_id="c1"),
        outcome=ToolOutcome("hello", True), tool_index=0, ref="1:0",
    )
    reply = deliver_turn_results(session, turn_state, 1)
    assert isinstance(reply, AssistantTurn) and reply.text == "ack"
    assert provider.chats_opened >= 1
    assert provider.fallback_prompts and "hello" in provider.fallback_prompts[0]


def test_native_protocol_error_answers_chain(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("CODEY_NATIVE_TOOLS", "1")
    provider = FakeStructuredProvider([
        AssistantTurn(text="", tool_calls=(
            ProviderToolCall(id="c1", name="read", arguments={"path": "app.py", "offset": "nope"}),
        )),
        AssistantTurn(text="", tool_calls=(
            ProviderToolCall(id="c2", name="done", arguments={"summary": "recovered"}),
        )),
    ])
    session: AgentLoopSession = _setup_loop(_request(provider, tmp_path))
    from codey.agents.prompt_context import initial_structured_reply

    result = _run_loop(session, initial_structured_reply(session), start_turn=1)
    assert result.stop_reason == "done"
    assert result.summary == "recovered"
    answered = provider.tool_results_seen[0][0]
    assert answered["tool_call_id"] == "c1"
    assert answered["content"].startswith("ERROR:")


def test_compaction_noop_cut_leaves_messages_untouched() -> None:
    from codey.agents import context_compaction as compaction

    messages: list[dict] = [
        {"role": "system", "content": "sys"},
        {"role": "assistant", "content": "", "tool_calls": [{"id": "c1", "function": {"name": "read"}}]},
    ]
    before = [dict(m) for m in messages]
    summary = compaction.compact_openai_messages_in_place(
        messages, context_window_tokens=10, reserve_tokens=9_000, keep_recent_tokens=100,
    )
    assert summary == ""
    assert messages == before


def test_research_bridge_protocol_error() -> None:
    from codey.research.native_bridge import answer_native_protocol_error

    seen: list = []

    class _Runner:
        def _send_native_tool_results(self, messages):
            seen.append(messages)
            return "next"

    class _Plan:
        protocol_error = "bad args"

    turn = AssistantTurn(text="", tool_calls=(
        ProviderToolCall(id="a", name="web_search", arguments={}),
        ProviderToolCall(id="b", name="open_url", arguments={}),
    ))
    assert answer_native_protocol_error(_Runner(), turn, _Plan()) is True
    assert [m["tool_call_id"] for m in seen[0]] == ["a", "b"]
    assert all(m["content"].startswith("ERROR:") for m in seen[0])
    assert answer_native_protocol_error(_Runner(), AssistantTurn(text="hi"), _Plan()) is False


def test_store_failure_is_audited(tmp_path: Path) -> None:
    from types import SimpleNamespace

    from codey.agents.tool_execution import maybe_externalize_large_tool_output

    class RefusingStore:
        def write_tool_output(self, **kwargs):
            return None

    class ExplodingStore:
        def write_tool_output(self, **kwargs):
            raise OSError("disk gone")

    for store, kind in ((RefusingStore(), "store_refused"), (ExplodingStore(), "OSError")):
        session = SimpleNamespace(
            request=SimpleNamespace(managed_outputs=store),
            session_id="s",
            run_id="r",
            profile=SimpleNamespace(name="coding_writer"),
        )
        outcome = maybe_externalize_large_tool_output(
            session, ToolCall(name="search", args={"query": "q"}), ToolOutcome("z" * 30_000, True),
            turn=1, tool_index=0,
        )
        assert outcome.truncated
        assert outcome.audit.get("managed_output_failed") is True
        assert outcome.audit.get("managed_output_failure") == kind
        assert not outcome.managed_output()
