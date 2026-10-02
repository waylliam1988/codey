"""Native delivery via the production task kernel.

Migrated from the old agent result-delivery codec to the production entry:
``run_task_kernel`` + ``execute_turn`` + ``_native_tool_messages`` +
``apply_recovery_first`` + ``KernelEffectSink``/``KernelRecordedProvider`` +
``TaskSession``/``TaskPolicy`` + ``normalize_turn``/``build_turn_snapshot``.
"""
from __future__ import annotations

from pathlib import Path
from unittest import mock

from codey.operations import kernel_transport
from codey.operations.kernel_execution import execute_turn
from codey.operations.kernel_protocol import build_turn_snapshot, normalize_turn
from codey.operations.kernel_recovery import apply_recovery_first
from codey.operations.task_effects import KernelEffectSink, KernelRecordedProvider
from codey.operations.task_loop import run_task_kernel
from codey.operations.task_session import TaskSession
from codey.policies.task_policy import TaskPolicy
from codey.providers.base import AssistantTurn, ProviderToolCall
from codey.providers.error_classification import ContextOverflowError
from codey.runtime.core.models import ToolCall, ToolResult
from codey.runtime.effects.tool_result_delivery import ToolResultDeliveryStore
from codey.runtime.log.session_log import RuntimeSessionLog
from codey.runtime.write.mutation_line import RuntimeMutationLine


class FakeStructuredProvider:
    name = "local"

    def __init__(self, turns: list[AssistantTurn]) -> None:
        self._turns = list(turns)
        self.tool_results_seen: list = []
        self.chats_opened = 0
        self.sent_prompts: list[str] = []

    def new_chat(self, timeout=None) -> None:
        self.chats_opened += 1

    def send(self, text: str, timeout=None) -> str:
        raise AssertionError("native path must not use text send()")

    def send_turn(self, prompt: str, tools=None, timeout=None) -> AssistantTurn:
        self.sent_prompts.append(str(prompt))
        return self._turns.pop(0)

    def send_tool_results(self, results, tools=None, timeout=None) -> AssistantTurn:
        self.tool_results_seen.append(list(results))
        return self._turns.pop(0)

    def close(self) -> None:
        return None


def _native_policy() -> TaskPolicy:
    return TaskPolicy(grants=frozenset({"project.read", "control"}))


def _native_session(tmp_path: Path, *, max_turns: int = 5) -> TaskSession:
    return TaskSession(
        policy=_native_policy(),
        task_kind="project",
        project=str(tmp_path),
        max_turns=max_turns,
    )


def _effect_harness(tmp_path: Path, session_id: str, run_id: str):
    log = RuntimeSessionLog(tmp_path / "state")
    line = RuntimeMutationLine(log)
    line.accept_operation(
        session_id=session_id,
        run_id=run_id,
        project=str(tmp_path),
        provider_id="local",
        turn_budget=10,
        max_repair_rounds=1,
        task_kind="project",
    )
    line.mark_writer_running(session_id, run_id, provider_id="local")
    sink = KernelEffectSink(line, session_id=session_id, run_id=run_id, provider_id="local")
    return log, line, sink


def test_native_delivery_records_effect_and_batch(tmp_path: Path) -> None:
    """Native read delivery records tool + provider effects and one batch."""
    (tmp_path / "app.py").write_text("hello\n", encoding="utf-8")
    provider = FakeStructuredProvider([
        AssistantTurn(text="", tool_calls=(ProviderToolCall(id="c1", name="read_file", arguments={"path": "app.py"}),)),
        AssistantTurn(text="", tool_calls=(ProviderToolCall(id="c2", name="done", arguments={"summary": "ok"}),)),
        AssistantTurn(text="ack"),
    ])
    log, _line, sink = _effect_harness(tmp_path, "s", "r")
    session = _native_session(tmp_path)
    snapshot = build_turn_snapshot(session, native=True)
    assert snapshot.native_tools
    names = {str((t.get("function") or {}).get("name") or "") for t in snapshot.native_tools}
    assert {"read_file", "done"} <= names
    recorded = KernelRecordedProvider(provider, sink)
    with mock.patch("codey.operations.kernel_transport.provider_uses_native", return_value=True):
        result = run_task_kernel(
            session,
            provider=recorded,
            executors={"read_file": lambda call: ToolResult(call=call, model_text="hello")},
            run_id="r",
            effect_scope="task",
            provider_id="local",
            project_path=tmp_path,
            user_task="read app",
            context_text="",
            intent_sink=sink,
        )
    assert result.stop_reason == "done"
    assert result.summary == "ok"
    assert result.completed
    # First native delivery carried the read result for c1.
    assert provider.tool_results_seen
    sent = provider.tool_results_seen[0][0]
    assert sent["role"] == "tool" and sent["tool_call_id"] == "c1"
    assert "hello" in str(sent["content"])
    # Done receipt closed the chain.
    assert any(
        m.get("tool_call_id") == "c2"
        for batch in provider.tool_results_seen[1:]
        for m in batch
    )
    from codey.runtime.effects.effect_records import RuntimeEffectStore

    effects = RuntimeEffectStore(log).load_effects("s", "r")
    assert any(
        row.intent.effect_category == "tool_call" and row.settlement is not None
        for row in effects
    )
    assert sum(1 for row in effects if row.intent.effect_category == "provider_send") >= 2
    batches = ToolResultDeliveryStore(log).load_batches("s", "r")
    assert len(batches) == 1
    assert batches[0].is_delivered


def test_native_overflow_fails_closed_without_fallback(tmp_path: Path) -> None:
    """Overflow during native delivery must terminate explicitly, not fallback.

    The unified kernel fails closed: exactly one delivery attempt, no text
    retry, receipts preserved.
    """
    from unittest import mock as _mock

    class OverflowProvider(FakeStructuredProvider):
        def __init__(self) -> None:
            super().__init__([AssistantTurn(text="ack")])
            self.fallback_prompts: list[str] = []
            self.tool_sends = 0

        def send_tool_results(self, results, tools=None, timeout=None) -> AssistantTurn:
            self.tool_sends += 1
            raise ContextOverflowError("full")

        def send_turn(self, prompt: str, tools=None, timeout=None) -> AssistantTurn:
            self.fallback_prompts.append(prompt)
            return self._turns.pop(0)

    provider = OverflowProvider()
    (tmp_path / "app.py").write_text("hello\n", encoding="utf-8")
    policy = TaskPolicy(grants=frozenset({"project.read", "control"}))
    session = TaskSession(policy=policy, task_kind="project", project=str(tmp_path), max_turns=3)
    provider._turns = [
        AssistantTurn(text="", tool_calls=(ProviderToolCall(id="c1", name="read_file", arguments={"path": "app.py"}),)),
    ]
    with _mock.patch("codey.operations.kernel_transport.provider_uses_native", return_value=True):
        result = run_task_kernel(
            session, provider=provider,
            executors={"read_file": lambda call: ToolResult(call=call, model_text="hello")},
            run_id="r-overflow-1", effect_scope="task",
            provider_id="local", project_path=tmp_path,
            user_task="read app", context_text="",
        )
    assert not result.completed
    assert result.stop_reason == "provider_failure", f"overflow must be provider_failure: {result}"
    # No retry of the failed delivery and no text fallback beyond the single
    # initial native prompt.
    assert provider.tool_sends == 1, f"overflow must not retry delivery, got {provider.tool_sends}"
    assert len(provider.fallback_prompts) == 1, (
        f"overflow must not fall back after failure, got {len(provider.fallback_prompts)} prompts"
    )


def test_native_protocol_error_answers_chain(tmp_path: Path) -> None:
    """Native call with bad args gets an ERROR receipt, then recovers."""
    (tmp_path / "app.py").write_text("hello\n", encoding="utf-8")
    bad = AssistantTurn(text="", tool_calls=(
        ProviderToolCall(id="c1", name="read_file", arguments={"path": "app.py", "offset": "nope"}),
    ))
    # Direct protocol check also uses the production normalizer.
    plan = normalize_turn(bad, policy=_native_policy(), controller_allowed=None)
    assert plan.protocol_error
    provider = FakeStructuredProvider([
        bad,
        AssistantTurn(text="", tool_calls=(
            ProviderToolCall(id="c2", name="done", arguments={"summary": "recovered"}),
        )),
        AssistantTurn(text="ack"),
    ])
    session = _native_session(tmp_path)
    with mock.patch("codey.operations.kernel_transport.provider_uses_native", return_value=True):
        result = run_task_kernel(
            session,
            provider=provider,
            executors={"read_file": lambda call: ToolResult(call=call, model_text="hello")},
            run_id="r-proto-1",
            effect_scope="task",
            provider_id="local",
            project_path=tmp_path,
            user_task="read app",
            context_text="",
        )
    assert result.stop_reason == "done"
    assert result.summary == "recovered"
    answered = provider.tool_results_seen[0][0]
    assert answered["tool_call_id"] == "c1"
    assert str(answered["content"]).startswith("ERROR:")


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


def test_strict_ledger_overflow_does_not_retry_same_batch(tmp_path: Path) -> None:
    """Overflow fails closed on the ledger: single NOT_SENT attempt, no retry."""
    (tmp_path / "app.py").write_text("hello\n", encoding="utf-8")

    class StrictOverflowProvider(FakeStructuredProvider):
        def __init__(self) -> None:
            super().__init__([
                AssistantTurn(text="", tool_calls=(
                    ProviderToolCall(id="c1", name="read_file", arguments={"path": "app.py"}),
                )),
            ])
            self.tool_sends = 0

        def send_turn(self, prompt: str, tools=None, timeout=None) -> AssistantTurn:
            self.sent_prompts.append(str(prompt))
            return self._turns.pop(0)

        def send_tool_results(self, results, tools=None, timeout=None) -> AssistantTurn:
            self.tool_sends += 1
            self.tool_results_seen.append(list(results))
            raise ContextOverflowError("full")

    provider = StrictOverflowProvider()
    log, _line, sink = _effect_harness(tmp_path, "sess-native-1", "run-native-1")
    recorded = KernelRecordedProvider(provider, sink)
    session = TaskSession(
        policy=_native_policy(), task_kind="project", project=str(tmp_path), max_turns=3,
    )
    with mock.patch("codey.operations.kernel_transport.provider_uses_native", return_value=True):
        result = run_task_kernel(
            session,
            provider=recorded,
            executors={"read_file": lambda call: ToolResult(call=call, model_text="hello")},
            run_id="run-native-1",
            effect_scope="task",
            provider_id="local",
            project_path=tmp_path,
            user_task="read app",
            context_text="",
            intent_sink=sink,
        )
    assert not result.completed
    assert result.stop_reason == "provider_failure"
    assert provider.tool_sends == 1
    from codey.runtime.effects.effect_records import SENT_STATE_NOT_SENT, RuntimeEffectStore

    effects = RuntimeEffectStore(log).load_effects("sess-native-1", "run-native-1")
    failed = [r for r in effects if r.intent.effect_category == "provider_send" and r.settlement is not None and r.settlement.status == "error"]
    assert failed
    assert failed[-1].settlement is not None
    assert failed[-1].settlement.sent_state == SENT_STATE_NOT_SENT
    batches = ToolResultDeliveryStore(log).load_batches("sess-native-1", "run-native-1")
    assert len(batches) == 1
    batch = batches[0]
    assert len(batch.send_attempts) == 1
    assert not batch.is_delivered


def test_recovered_native_delivery_marks_delivered(tmp_path: Path) -> None:
    """Recovered results are delivered natively before new model calls."""
    from codey.operations import kernel_prompt as _prompt

    (tmp_path / "app.py").write_text("hello\n", encoding="utf-8")
    session = _native_session(tmp_path)
    pending = [
        ToolResult(call=ToolCall(name="read_file", args={"path": "app.py"}, call_id="c1"), model_text="hello"),
    ]
    messages = kernel_transport._native_tool_messages(pending, session)
    assert len(messages) == 1 and messages[0]["tool_call_id"] == "c1"
    prompt, recovered = apply_recovery_first(
        session,
        True,
        pending,
        "BASE",
        None,
        provider_session_changed=False,
        format_results=_prompt._format_results,
        native_tool_messages=kernel_transport._native_tool_messages,
    )
    assert recovered == messages
    provider = FakeStructuredProvider([
        AssistantTurn(text="", tool_calls=(ProviderToolCall(id="d", name="done", arguments={"summary": "ok"}),)),
        AssistantTurn(text="ack"),
    ])
    session2 = _native_session(tmp_path)
    with mock.patch("codey.operations.kernel_transport.provider_uses_native", return_value=True):
        result = run_task_kernel(
            session2,
            provider=provider,
            executors={"read_file": lambda call: ToolResult(call=call, model_text="hello")},
            run_id="r-recovered-1",
            effect_scope="task",
            provider_id="local",
            project_path=tmp_path,
            user_task="read app",
            context_text="",
            initial_results=pending,
        )
    assert result.stop_reason == "done"
    assert provider.tool_results_seen
    assert provider.tool_results_seen[0][0]["tool_call_id"] == "c1"
    assert provider.tool_results_seen[0][0]["role"] == "tool"


def test_native_success_leaves_no_pending_context_rows(tmp_path: Path) -> None:
    """Successful native delivery leaves no pending chain: done closes cleanly."""
    (tmp_path / "app.py").write_text("hello\n", encoding="utf-8")
    provider = FakeStructuredProvider([
        AssistantTurn(text="", tool_calls=(ProviderToolCall(id="c1", name="read_file", arguments={"path": "app.py"}),)),
        AssistantTurn(text="", tool_calls=(ProviderToolCall(id="c9", name="done", arguments={"summary": "ok"}),)),
        AssistantTurn(text="ack"),
    ])
    session = _native_session(tmp_path)
    with mock.patch("codey.operations.kernel_transport.provider_uses_native", return_value=True):
        result = run_task_kernel(
            session,
            provider=provider,
            executors={"read_file": lambda call: ToolResult(call=call, model_text="hello")},
            run_id="r-clean-1",
            effect_scope="task",
            provider_id="local",
            project_path=tmp_path,
            user_task="read app",
            context_text="",
        )
    assert result.stop_reason == "done"
    assert session.last_done_text == "ok"
    assert len(provider.tool_results_seen) == 2
    assert session.executed
    assert all(isinstance(record, dict) and record.get("ok") for record in session.executed.values())


def test_native_too_many_calls_answered_in_full(tmp_path: Path) -> None:
    from codey.toolchain.definition import MAX_ACCIDENTAL_TOOL_CALLS

    (tmp_path / "app.py").write_text("hello\n", encoding="utf-8")
    calls = tuple(
        ProviderToolCall(id=f"c{i}", name="read_file", arguments={"path": f"f{i}.py"})
        for i in range(MAX_ACCIDENTAL_TOOL_CALLS + 1)
    )
    provider = FakeStructuredProvider([
        AssistantTurn(text="", tool_calls=calls),
        AssistantTurn(text="", tool_calls=(ProviderToolCall(id="d", name="done", arguments={"summary": "ok"}),)),
        AssistantTurn(text="ack"),
    ])
    session = _native_session(tmp_path)
    with mock.patch("codey.operations.kernel_transport.provider_uses_native", return_value=True):
        result = run_task_kernel(
            session,
            provider=provider,
            executors={"read_file": lambda call: ToolResult(call=call, model_text="hello")},
            run_id="r-many-1",
            effect_scope="task",
            provider_id="local",
            project_path=tmp_path,
            user_task="read app",
            context_text="",
        )
    assert result.stop_reason == "done"
    answered = provider.tool_results_seen[0]
    assert [m["tool_call_id"] for m in answered] == [f"c{i}" for i in range(MAX_ACCIDENTAL_TOOL_CALLS + 1)]
    assert all(str(m["content"]).startswith("ERROR:") for m in answered)


def test_idless_turn_restarts_fresh_chat_instead_of_dangling(tmp_path: Path) -> None:
    """Idless native calls fail closed without dangling ids; next done closes."""
    (tmp_path / "app.py").write_text("hello\n", encoding="utf-8")
    provider = FakeStructuredProvider([
        AssistantTurn(text="", tool_calls=(ProviderToolCall(id="", name="read_file", arguments={"path": "app.py"}),)),
        AssistantTurn(text="", tool_calls=(ProviderToolCall(id="d", name="done", arguments={"summary": "ok"}),)),
        AssistantTurn(text="ack"),
    ])
    opened_at_start = provider.chats_opened
    session = _native_session(tmp_path)
    with mock.patch("codey.operations.kernel_transport.provider_uses_native", return_value=True):
        result = run_task_kernel(
            session,
            provider=provider,
            executors={"read_file": lambda call: ToolResult(call=call, model_text="hello")},
            run_id="r-idless-1",
            effect_scope="task",
            provider_id="local",
            project_path=tmp_path,
            user_task="read app",
            context_text="",
        )
    assert result.stop_reason == "done"
    # No fresh-chat restart: idless fails closed as a protocol error.
    assert provider.chats_opened == opened_at_start
    # Repair prompt went through send_turn again.
    assert len(provider.sent_prompts) >= 2
    # Only the valid done id needed a receipt.
    assert provider.tool_results_seen
    assert provider.tool_results_seen[-1][0]["tool_call_id"] == "d"


def test_native_mixed_done_answered_in_full(tmp_path: Path) -> None:
    (tmp_path / "app.py").write_text("hello\n", encoding="utf-8")
    calls = (
        ProviderToolCall(id="d", name="done", arguments={"summary": "bye"}),
        ProviderToolCall(id="r", name="read_file", arguments={"path": "app.py"}),
    )
    provider = FakeStructuredProvider([
        AssistantTurn(text="", tool_calls=calls),
        AssistantTurn(text="", tool_calls=(ProviderToolCall(id="d2", name="done", arguments={"summary": "ok"}),)),
        AssistantTurn(text="ack"),
    ])
    session = _native_session(tmp_path)
    with mock.patch("codey.operations.kernel_transport.provider_uses_native", return_value=True):
        result = run_task_kernel(
            session,
            provider=provider,
            executors={"read_file": lambda call: ToolResult(call=call, model_text="hello")},
            run_id="r-mixed-1",
            effect_scope="task",
            provider_id="local",
            project_path=tmp_path,
            user_task="read app",
            context_text="",
        )
    assert result.stop_reason == "done"
    answered = provider.tool_results_seen[0]
    assert [m["tool_call_id"] for m in answered] == ["d", "r"]
    assert all(str(m["content"]).startswith("ERROR:") for m in answered)


def test_native_helpers_execute_and_format(tmp_path: Path) -> None:
    """Direct production helpers: execute_turn + snapshot + transport."""
    session = _native_session(tmp_path)
    snapshot = build_turn_snapshot(session, native=True)
    assert snapshot.native_tools
    results = execute_turn(
        session,
        [ToolCall(name="read_file", args={"path": "app.py"}, call_id="c0")],
        executors={"read_file": lambda call: ToolResult(call=call, model_text="hello")},
        run_id="r-helper-1",
        turn=1,
    )
    assert results and results[0].model_text == "hello"
    plan = normalize_turn(
        AssistantTurn(text="", tool_calls=(ProviderToolCall(id="c0", name="read_file", arguments={"path": "app.py"}),)),
        policy=session.policy,
        controller_allowed=None,
    )
    assert not plan.protocol_error
    assert plan.calls[0].call_id == "c0"
    messages = kernel_transport._native_tool_messages(results, session)
    assert messages and messages[0]["tool_call_id"] == "c0"
    import pytest as _pytest

    mixed = [
        ToolResult(call=ToolCall(name="read_file", args={"path": "a.py"}, call_id="c1"), model_text="a"),
        ToolResult(call=ToolCall(name="read_file", args={"path": "b.py"}, call_id=""), model_text="b"),
    ]
    with _pytest.raises(ValueError):
        kernel_transport._native_tool_messages(mixed, session)


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
    assert "malformed" in turn.text
    assert "here" not in turn.text
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


def test_large_receipt_store_failure_never_creates_an_inline_success() -> None:
    import pytest

    from codey.operations.kernel_receipts import result_receipt_fields
    from codey.runtime.core.models import ToolResult

    class RefusingStore:
        def write_tool_output(self, **kwargs):
            return None

    class ExplodingStore:
        def write_tool_output(self, **kwargs):
            raise OSError("disk gone")

    result = ToolResult(ToolCall("search", {"query": "q"}), "z" * 30_000)
    for store, error in ((RefusingStore(), ValueError), (ExplodingStore(), OSError)):
        with pytest.raises(error):
            result_receipt_fields(result, store=store, session_id="s", run_id="r", effect_id="e")
