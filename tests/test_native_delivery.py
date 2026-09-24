from __future__ import annotations

from pathlib import Path

from codey.agents.loop import _run_loop, _setup_loop
from codey.agents.request import AgentRequest
from codey.agents.result_delivery import deliver_recovered_results, deliver_turn_results
from codey.agents.state import AgentLoopSession
from codey.agents.tool_execution import TurnState, record_tool_outcome
from codey.env_names import NATIVE_TOOLS_ENV
from codey.providers.base import AssistantTurn, ProviderToolCall
from codey.providers.error_classification import ContextOverflowError
from codey.runtime.core.models import ToolCall
from codey.runtime.effects.tool_result_delivery import ToolResultDeliveryStore
from codey.runtime.log.session_log import RuntimeSessionLog
from codey.runtime.write.mutation_line import RuntimeMutationLine
from codey.toolchain.runtime import ToolOutcome


class FakeMutations:
    def __init__(self) -> None:
        self.batches: list = []
        self.begins: list = []
        self.settlements: list = []

    def begin_tool_batch(self, session_id, run_id, *, intents=(), delivery_intent=None):
        self.batches.append(delivery_intent)

    def begin_provider_effect(self, session_id, run_id, intent, *, driver=None, delivery_batch_id="",
                              supersede_effect_id=""):
        self.begins.append((intent, delivery_batch_id, supersede_effect_id))
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
    monkeypatch.setenv(NATIVE_TOOLS_ENV, "1")
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
    intent, batch_id, supersede = mutations.begins[0]
    assert batch_id == turn_state.delivery_batch_id
    assert supersede == ""
    assert batch_id
    assert len(mutations.settlements) == 1
    assert getattr(mutations.settlements[0], "status", "") == "ok"
    sent = provider.tool_results_seen[0][0]
    assert sent["role"] == "tool" and sent["tool_call_id"] == "c1"


def test_native_overflow_falls_back_to_text(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv(NATIVE_TOOLS_ENV, "1")

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
    monkeypatch.setenv(NATIVE_TOOLS_ENV, "1")
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


def _strict_ledger_session(provider, tmp_path: Path, monkeypatch) -> AgentLoopSession:
    monkeypatch.setenv(NATIVE_TOOLS_ENV, "1")
    state_dir = tmp_path / "state"
    log = RuntimeSessionLog(state_dir)
    line = RuntimeMutationLine(log)
    line.accept_operation(
        session_id="sess-native-1",
        run_id="run-native-1",
        project=str(tmp_path / "proj"),
        provider_id="local",
        turn_budget=10,
        max_repair_rounds=1,
        task_kind="project",
    )
    line.mark_writer_running("sess-native-1", "run-native-1", provider_id="local")
    return _setup_loop(_request(
        provider,
        tmp_path / "proj",
        session_id="sess-native-1",
        run_id="run-native-1",
        runtime_mutations=line,
        tool_result_delivery=ToolResultDeliveryStore(log),
    )), log


def _recorded_turn_state(session: AgentLoopSession, *, call_id: str = "c1") -> TurnState:
    turn_state = TurnState()
    record_tool_outcome(
        session, turn_state, turn=1,
        call=ToolCall(name="read", args={"path": "app.py"}, call_id=call_id),
        outcome=ToolOutcome("hello", True), tool_index=0, ref="1:0",
    )
    return turn_state


def test_strict_ledger_overflow_retries_same_batch(monkeypatch, tmp_path: Path) -> None:
    """First attempt overflows (NOT_SENT + voided), the text retry delivers."""
    seen_sends: list = []

    class StrictOverflowProvider(FakeStructuredProvider):
        def __init__(self) -> None:
            super().__init__([AssistantTurn(text="ack")])

        def send_tool_results(self, results, tools=None, timeout=None) -> AssistantTurn:
            seen_sends.append(("tool", list(results)))
            raise ContextOverflowError("full")

        def send_turn(self, prompt: str, tools=None, timeout=None) -> AssistantTurn:
            seen_sends.append(("turn", prompt))
            return self._turns.pop(0)

    provider = StrictOverflowProvider()
    session, log = _strict_ledger_session(provider, tmp_path, monkeypatch)
    reply = deliver_turn_results(session, _recorded_turn_state(session), 1)
    assert isinstance(reply, AssistantTurn) and reply.text == "ack"
    assert [kind for kind, _ in seen_sends] == ["tool", "turn"]
    assert provider.chats_opened >= 1
    store = ToolResultDeliveryStore(log)
    batches = store.load_batches("sess-native-1", "run-native-1")
    assert len(batches) == 1
    batch = batches[0]
    assert len(batch.send_attempts) == 2
    assert len(batch.superseded_effect_ids) == 1
    assert batch.superseded_effect_ids[0] == batch.send_attempts[0]
    assert batch.is_delivered
    assert batch.delivered_effect_ids == (batch.send_attempts[1],)
    assert batch.active_attempts == (batch.send_attempts[1],)


def test_recovered_native_delivery_marks_delivered(monkeypatch, tmp_path: Path) -> None:
    provider = FakeStructuredProvider([
        AssistantTurn(text="", tool_calls=(ProviderToolCall(id="c9", name="done", arguments={"summary": "ok"}),)),
    ])
    session, log = _strict_ledger_session(provider, tmp_path, monkeypatch)
    reply = deliver_recovered_results(
        session, _recorded_turn_state(session), turn=1, recovered_batch_id="",
    )
    assert isinstance(reply, AssistantTurn)
    store = ToolResultDeliveryStore(log)
    batches = store.load_batches("sess-native-1", "run-native-1")
    assert len(batches) == 1
    assert batches[0].is_delivered is True
    assert len(batches[0].send_attempts) == 1
    sent = provider.tool_results_seen[0][0]
    assert sent["role"] == "tool" and sent["tool_call_id"] == "c1"


def test_native_success_leaves_no_pending_context_rows(monkeypatch, tmp_path: Path) -> None:
    from unittest import mock

    provider = FakeStructuredProvider([
        AssistantTurn(text="", tool_calls=(ProviderToolCall(id="c9", name="done", arguments={"summary": "ok"}),)),
    ])
    session, _log = _strict_ledger_session(provider, tmp_path, monkeypatch)
    with mock.patch(
        "codey.agents.result_delivery.build_next_tool_prompt",
        side_effect=AssertionError("lazy fallback must not build on success"),
    ):
        reply = deliver_turn_results(session, _recorded_turn_state(session), 1)
    assert isinstance(reply, AssistantTurn)
    assert session.pending_context_rows == []
    assert session.pending_repair_sections == []


def test_native_too_many_calls_answered_in_full(monkeypatch, tmp_path: Path) -> None:
    from codey.toolchain.definition import MAX_ACCIDENTAL_TOOL_CALLS

    calls = tuple(
        ProviderToolCall(id=f"c{i}", name="read", arguments={"path": f"f{i}.py"})
        for i in range(MAX_ACCIDENTAL_TOOL_CALLS + 1)
    )
    provider = FakeStructuredProvider([
        AssistantTurn(text="", tool_calls=calls),
        AssistantTurn(text="", tool_calls=(ProviderToolCall(id="d", name="done", arguments={"summary": "ok"}),)),
    ])
    session, _log = _strict_ledger_session(provider, tmp_path, monkeypatch)
    from codey.agents.prompt_context import initial_structured_reply

    result = _run_loop(session, initial_structured_reply(session), start_turn=1)
    assert result.stop_reason == "done"
    answered = provider.tool_results_seen[0]
    assert [m["tool_call_id"] for m in answered] == [f"c{i}" for i in range(MAX_ACCIDENTAL_TOOL_CALLS + 1)]
    assert all(m["content"].startswith("ERROR:") for m in answered)


def test_idless_turn_restarts_fresh_chat_instead_of_dangling(monkeypatch, tmp_path: Path) -> None:
    turns = [
        AssistantTurn(text="", tool_calls=(ProviderToolCall(id="", name="read", arguments={"path": "app.py"}),)),
        AssistantTurn(text="", tool_calls=(ProviderToolCall(id="d", name="done", arguments={"summary": "ok"}),)),
    ]
    sent_turns: list[str] = []

    class FreshChatProvider(FakeStructuredProvider):
        def send_turn(self, prompt: str, tools=None, timeout=None) -> AssistantTurn:
            sent_turns.append(prompt)
            return self._turns.pop(0)

    provider = FreshChatProvider(turns)
    session, _log = _strict_ledger_session(provider, tmp_path, monkeypatch)
    from codey.agents.prompt_context import initial_structured_reply

    first = initial_structured_reply(session)
    opened_at_start = provider.chats_opened
    result = _run_loop(session, first, start_turn=1)
    assert result.stop_reason == "done"
    assert provider.chats_opened == opened_at_start + 1
    assert provider.tool_results_seen == []
    assert len(sent_turns) == 2
    assert "read app" in sent_turns[1]
    assert "app.py" in sent_turns[1]


def test_native_mixed_done_answered_in_full(monkeypatch, tmp_path: Path) -> None:
    calls = (
        ProviderToolCall(id="d", name="done", arguments={"summary": "bye"}),
        ProviderToolCall(id="r", name="read", arguments={"path": "app.py"}),
    )
    provider = FakeStructuredProvider([
        AssistantTurn(text="", tool_calls=calls),
        AssistantTurn(text="", tool_calls=(ProviderToolCall(id="d2", name="done", arguments={"summary": "ok"}),)),
    ])
    session, _log = _strict_ledger_session(provider, tmp_path, monkeypatch)
    from codey.agents.prompt_context import initial_structured_reply

    result = _run_loop(session, initial_structured_reply(session), start_turn=1)
    assert result.stop_reason == "done"
    answered = provider.tool_results_seen[0]
    assert [m["tool_call_id"] for m in answered] == ["d", "r"]
    assert all(m["content"].startswith("ERROR:") for m in answered)


def test_local_malformed_tool_calls_fail_closed(monkeypatch) -> None:
    import json as _json

    from codey.providers import local_openai as local_module
    from codey.providers.local_openai import LocalOpenAIProvider

    bodies: list[dict] = []

    class _FakeResponse:
        def __init__(self, payload: bytes) -> None:
            self._payload = payload

        def read(self, size: int | None = None) -> bytes:
            del size
            return self._payload

        def __enter__(self) -> _FakeResponse:
            return self

        def __exit__(self, *exc: object) -> bool:
            return False

    def _body(message: dict) -> dict:
        return {"choices": [{"finish_reason": "tool_calls", "message": message}]}

    def _install(body: dict):
        def fake_urlopen(request, timeout=None):
            bodies.append(_json.loads(request.data.decode("utf-8")))
            return _FakeResponse(_json.dumps(body).encode("utf-8"))

        monkeypatch.setattr(local_module.urllib.request, "urlopen", fake_urlopen)

    provider = LocalOpenAIProvider(base_url="http://127.0.0.1:9/v1", model="qwen-test")
    _install(_body({
        "content": "here",
        "tool_calls": [
            {"id": "", "type": "function", "function": {"name": "read", "arguments": "{}"}},
            {"type": "function"},
        ],
    }))
    turn = provider.send_turn("do it", None)
    assert turn.tool_calls == ()
    assert turn.text == "here"
    assert all("tool_calls" not in m for m in provider._messages)

    _install(_body({"content": "", "tool_calls": [{"id": "", "type": "function"}]}))
    turn = provider.send_turn("again", None)
    assert turn.tool_calls == ()
    assert "malformed" in turn.text
    assert all("tool_calls" not in m for m in provider._messages)


def _commit_entries(log: RuntimeSessionLog, session_id: str, entries) -> None:
    from codey.runtime.log.entries import RuntimeLogEntry

    path = log.path_for(session_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("ab") as handle:
        for entry in entries:
            row = RuntimeLogEntry(
                session_id=session_id,
                lane=entry.get("lane"),
                operation_id=entry.get("operation_id"),
                kind=entry.get("kind"),
                payload=entry.get("payload"),
            )
            handle.write(row.to_json_line().encode("utf-8"))


def _delivery_harness(tmp_path: Path):
    from codey.runtime.effects.tool_result_delivery import (
        DeliveryBatchIntent,
        DeliveryBatchItem,
        compute_batch_digest,
    )

    session_id, run_id = "sess-void-1", "run-void-1"
    log = RuntimeSessionLog(tmp_path / "state")
    RuntimeMutationLine(log).accept_operation(
        session_id=session_id, run_id=run_id, project=str(tmp_path),
        provider_id="local", turn_budget=10, max_repair_rounds=1, task_kind="project",
    )
    items = (DeliveryBatchItem(tool_index=0, tool_name="read", ref="r0", replay_class="safe", is_denied=False),)
    intent = DeliveryBatchIntent(
        batch_id="batch-void-1", session_id=session_id, run_id=run_id, turn=1,
        items=items, batch_digest=compute_batch_digest(items),
    )
    from codey.runtime.effects.tool_result_delivery import batch_intent_entry

    _commit_entries(log, session_id, (batch_intent_entry(intent),))
    return log, session_id, run_id


def test_supersede_entry_projection_semantics(tmp_path: Path) -> None:
    import pytest

    from codey.runtime.effects.tool_result_delivery import (
        ToolResultDeliveryError,
        send_attempt_entry,
        send_superseded_entry,
    )

    log, session_id, run_id = _delivery_harness(tmp_path)
    store = ToolResultDeliveryStore(log)
    batches = lambda: store.load_batches(session_id, run_id)  # noqa: E731

    with pytest.raises(ToolResultDeliveryError):
        send_superseded_entry(session_id, run_id, batch_id="nope", provider_effect_id="e1", batches=batches())
    with pytest.raises(ToolResultDeliveryError):
        send_superseded_entry(session_id, run_id, batch_id="batch-void-1", provider_effect_id="e1",
                              batches=batches())

    attempt = send_attempt_entry(session_id, run_id, batch_id="batch-void-1", provider_effect_id="e1",
                                 batches=batches())
    _commit_entries(log, session_id, (attempt,))
    voided = send_superseded_entry(session_id, run_id, batch_id="batch-void-1", provider_effect_id="e1",
                                   batches=batches())
    assert voided is not None
    _commit_entries(log, session_id, (voided,))
    assert send_superseded_entry(session_id, run_id, batch_id="batch-void-1", provider_effect_id="e1",
                                 batches=batches()) is None
    retry = send_attempt_entry(session_id, run_id, batch_id="batch-void-1", provider_effect_id="e2",
                               batches=batches())
    assert retry is not None
    _commit_entries(log, session_id, (retry,))
    live = batches()[0]
    assert live.active_attempts == ("e2",)
    assert live.can_recover_before_provider_send is False
    with pytest.raises(ToolResultDeliveryError):
        send_attempt_entry(session_id, run_id, batch_id="batch-void-1", provider_effect_id="e3",
                           batches=batches())

    from codey.runtime.effects.tool_result_delivery import delivered_entry

    done = delivered_entry(session_id, run_id, batch_id="batch-void-1", provider_effect_id="e2",
                           batches=batches())
    assert done is not None
    _commit_entries(log, session_id, (done,))
    with pytest.raises(ToolResultDeliveryError):
        send_superseded_entry(session_id, run_id, batch_id="batch-void-1", provider_effect_id="e2",
                              batches=batches())
    with pytest.raises(ToolResultDeliveryError):
        delivered_entry(session_id, run_id, batch_id="batch-void-1", provider_effect_id="e1",
                        batches=batches())


def _proof_harness(tmp_path: Path, tag: str):
    from codey.runtime.effects.tool_result_delivery import (
        DeliveryBatchIntent,
        DeliveryBatchItem,
        compute_batch_digest,
    )

    session_id, run_id = f"sess-proof-{tag}", f"run-proof-{tag}"
    log = RuntimeSessionLog(tmp_path / "state")
    line = RuntimeMutationLine(log)
    line.accept_operation(
        session_id=session_id, run_id=run_id, project=str(tmp_path),
        provider_id="local", turn_budget=10, max_repair_rounds=1, task_kind="project",
    )
    line.mark_writer_running(session_id, run_id, provider_id="local")

    def _batch(batch_id: str) -> None:
        items = (DeliveryBatchItem(tool_index=0, tool_name="read", ref="r0",
                                   replay_class="safe", is_denied=False),)
        line.begin_tool_batch(
            session_id, run_id, intents=(),
            delivery_intent=DeliveryBatchIntent(
                batch_id=batch_id, session_id=session_id, run_id=run_id, turn=1,
                items=items, batch_digest=compute_batch_digest(items)),
        )

    return log, line, session_id, run_id, _batch


def _provider_intent(effect_id: str, session_id: str, run_id: str):
    from codey.runtime.effects.effect_records import EFFECT_CATEGORY_PROVIDER_SEND, RuntimeEffectIntent
    from codey.runtime.effects.replay_policy import ReplayClass

    return RuntimeEffectIntent(
        effect_id=effect_id, effect_category=EFFECT_CATEGORY_PROVIDER_SEND,
        session_id=session_id, run_id=run_id, phase="writer", provider_id="local",
        turn=1, replay_class=ReplayClass.UNSAFE)


def _provider_settlement(effect_id: str, session_id: str, run_id: str, *, status: str, sent_state: str):
    from codey.runtime.effects.effect_records import EFFECT_CATEGORY_PROVIDER_SEND, RuntimeEffectSettlement

    return RuntimeEffectSettlement(
        effect_id=effect_id, effect_category=EFFECT_CATEGORY_PROVIDER_SEND,
        session_id=session_id, run_id=run_id, status=status, sent_state=sent_state)


def test_builder_supersede_requires_not_sent_proof(tmp_path: Path) -> None:
    import pytest

    from codey.runtime.core.operation_state import RuntimeOperationTransitionError
    from codey.runtime.effects.effect_records import (
        SENT_STATE_MAYBE_SENT,
        SENT_STATE_NOT_SENT,
        SETTLEMENT_STATUS_ERROR,
        SETTLEMENT_STATUS_OK,
    )
    from codey.runtime.effects.tool_result_delivery import ToolResultDeliveryStore

    log, line, session_id, run_id, _batch = _proof_harness(tmp_path, "a")
    _batch("b-unproven")
    line.begin_provider_effect(session_id, run_id, _provider_intent("e1", session_id, run_id),
                               delivery_batch_id="b-unproven")
    with pytest.raises(RuntimeOperationTransitionError):
        line.begin_provider_effect(session_id, run_id, _provider_intent("e2", session_id, run_id),
                                   delivery_batch_id="b-unproven", supersede_effect_id="e1")
    line.settle_provider_effect(
        session_id, run_id,
        _provider_settlement("e1", session_id, run_id, status=SETTLEMENT_STATUS_ERROR,
                             sent_state=SENT_STATE_MAYBE_SENT))
    with pytest.raises(RuntimeOperationTransitionError):
        line.begin_provider_effect(session_id, run_id, _provider_intent("e2", session_id, run_id),
                                   delivery_batch_id="b-unproven", supersede_effect_id="e1")

    _batch("b-proven")
    line.begin_provider_effect(session_id, run_id, _provider_intent("e3", session_id, run_id),
                               delivery_batch_id="b-proven")
    line.settle_provider_effect(
        session_id, run_id,
        _provider_settlement("e3", session_id, run_id, status=SETTLEMENT_STATUS_ERROR,
                             sent_state=SENT_STATE_NOT_SENT))
    line.begin_provider_effect(session_id, run_id, _provider_intent("e4", session_id, run_id),
                               delivery_batch_id="b-proven", supersede_effect_id="e3")
    line.settle_provider_effect(
        session_id, run_id,
        _provider_settlement("e4", session_id, run_id, status=SETTLEMENT_STATUS_OK,
                             sent_state="settled"))
    store = ToolResultDeliveryStore(log)
    batches = {b.intent.batch_id: b for b in store.load_batches(session_id, run_id)}
    proven = batches["b-proven"]
    assert proven.superseded_effect_ids == ("e3",)
    assert proven.active_attempts == ("e4",)
    assert proven.is_delivered
    assert batches["b-unproven"].active_attempts == ("e1",)
    assert batches["b-unproven"].is_delivered is False


def test_voided_only_batch_stays_recoverable(tmp_path: Path) -> None:
    from codey.runtime.effects.tool_result_delivery import send_attempt_entry, send_superseded_entry

    log, session_id, run_id = _delivery_harness(tmp_path)
    store = ToolResultDeliveryStore(log)
    attempt = send_attempt_entry(session_id, run_id, batch_id="batch-void-1", provider_effect_id="e1",
                                 batches=store.load_batches(session_id, run_id))
    _commit_entries(log, session_id, (attempt,))
    voided = send_superseded_entry(session_id, run_id, batch_id="batch-void-1", provider_effect_id="e1",
                                   batches=store.load_batches(session_id, run_id))
    _commit_entries(log, session_id, (voided,))
    batch = store.load_batches(session_id, run_id)[0]
    assert batch.active_attempts == ()
    assert batch.is_delivered is False
    assert batch.can_recover_before_provider_send is True


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
