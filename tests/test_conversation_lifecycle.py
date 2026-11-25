"""会话生命周期必须真实一致：新会话成功才清空窗口，继续会话保留预算。

- fresh_chat 且 new_chat 成功 → begin_window 后再首发；
- fresh_chat 但 new_chat 失败 → 停止并报 provider failure，不向旧会话发送；
- 非 fresh_chat → 保留原窗口与累计预算，不得清空。
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import closing, contextmanager
from dataclasses import replace
from pathlib import Path
from typing import Never

from codey.agents.handoff import ConversationContext
from codey.operations.context import RunFrame, RunHooks, RunWork
from codey.operations.task_run import TaskRunDeps
from codey.runtime.observe.events import RunEvent
from tests.support.task_entry_helpers import entry_frame


def _conversation() -> ConversationContext:
    conv = ConversationContext()
    conv.estimated_context_tokens = 12000
    return conv


def test_native_exchange_accounts_for_tool_arguments() -> None:
    from types import SimpleNamespace

    from codey.operations.provider_session import ConversationProvider
    from codey.providers.base import AssistantTurn, ProviderToolCall

    exchanges: list[tuple[str, str]] = []
    turn = AssistantTurn(tool_calls=(ProviderToolCall("id", "edit", {"content": "x" * 3000}),))

    def send_turn(*_args: object) -> AssistantTurn:
        return turn

    class Recorder:
        def begin_window(self, provider_id: str, mode: str, project: str) -> None:
            raise AssertionError("Sending one tool turn must reuse the window")

        def record_exchange(self, prompt: str, reply: str) -> None:
            exchanges.append((prompt, reply))

    provider = ConversationProvider(SimpleNamespace(send_turn=send_turn), Recorder())
    assert provider.send_turn("prompt", []) is turn
    assert len(exchanges) == 1
    assert "x" * 3000 in exchanges[0][1]


def test_begin_window_only_after_successful_new_chat() -> None:
    conv = _conversation()
    conv.estimated_context_tokens = 12000

    from codey.operations.task_entry import _open_fresh_session

    provider = _TextProvider(fail_new_chat=True)
    outcome = _open_fresh_session(conv, provider, provider_id="local", kind="hybrid")
    assert outcome.ok is False, "新会话打开失败必须停止"
    assert provider.calls == ["new_chat"], f"失败后不得向旧会话发送：{provider.calls}"
    assert conv.estimated_context_tokens == 12000, "失败不得清空原预算"


def test_successful_new_chat_resets_window_before_first_send() -> None:
    conv = _conversation()

    from codey.operations.task_entry import _open_fresh_session

    provider = _TextProvider()
    outcome = _open_fresh_session(conv, provider, provider_id="local", kind="hybrid")
    assert outcome.ok is True
    assert provider.calls == ["new_chat"]
    assert conv.estimated_context_tokens == 0
    assert conv.snapshot.conversation_summary == ""


def _entry_frame(*, fresh_chat: bool, conversation: ConversationContext, provider: _TextProvider) -> RunFrame:
    from codey.policies.task_policy import TaskPolicy
    from codey.task.model import TaskSubmission

    request = TaskSubmission("s-life", None, "do things", 1, False, "local", intent="chat", run_id="r-life")
    return replace(entry_frame(request), task_kind="chat", provider=provider,
        conversation=conversation, fresh_chat=fresh_chat,
        prior_snapshot=conversation.snapshot, entry_policy=TaskPolicy(grants=frozenset({"control"})),
    )


def _unexpected(*_args: object, **_kwargs: object) -> Never:
    raise AssertionError("Conversation lifecycle must not dispatch project operations")


@contextmanager
def _entry_deps(root: Path) -> Iterator[TaskRunDeps]:
    from codey.app.headless_runner import HeadlessAppContext

    with closing(HeadlessAppContext(root, port=0, emit_jsonl=lambda _row: None)) as state:
        durable = state.durable
        durable.admit_input(conversation_id="s-life", request_id="input", task_id="r-life",
                            text="do things", provider_snapshot={})
        durable.claim_task("r-life")
        durable.start_task("r-life", state=dict(durable.task("r-life").state))
        state.runtime_mutations.accept_operation(session_id="s-life", run_id="r-life", provider_id="local",
            turn_budget=1, max_repair_rounds=0, task_kind="chat")
        yield TaskRunDeps(state=state, agent_run=_unexpected, collect_changes=_unexpected, run_review=_unexpected,
            capture_provider_failure=_unexpected, workspace_revisions=state.workspace_revisions,
            runtime_mutations=state.runtime_mutations, managed_outputs=state.managed_outputs)


def _entry_hooks() -> RunHooks:
    def ignore_event(_event: RunEvent) -> None:
        pass

    return RunHooks(on_event=ignore_event, on_shell_request=_unexpected, update_checkpoint=_unexpected,
        record_provider_failure=_unexpected, append_ledger=_unexpected, provider_failover_order=lambda: (), supervisor=None)


def _entry_work() -> RunWork:
    from codey.runtime.observe.execution_evidence import ExecutionEvidence

    return RunWork(
        recent_events=[],
        evidence=ExecutionEvidence(workspace_revision=0, workspace_fingerprint=""),
    )


class _TextProvider:
    name = "scripted"

    @property
    def location(self) -> str:
        return "scripted"

    def __init__(self, *, fail_new_chat: bool = False) -> None:
        self.calls: list[str] = []
        self.fail_new_chat = fail_new_chat

    def new_chat(self, timeout: float | None = None) -> None:
        self.calls.append("new_chat")
        if self.fail_new_chat:
            raise RuntimeError("tab gone")

    def send(self, text: str, timeout: float | None = None) -> str:
        self.calls.append("send")
        return '{"tool": "done", "args": {"summary": "did stuff"}}'

    def close(self) -> None:
        pass


def test_entry_kernel_continuing_run_keeps_budget_and_summary(tmp_path: Path) -> None:
    from unittest import mock

    from codey.operations.task_entry import run_entry_kernel

    conv = _conversation()
    conv.begin_window("local", "chat", "")
    conv.estimated_context_tokens = 12000
    conv.snapshot = replace(conv.snapshot, conversation_summary="prior summary")
    provider = _TextProvider()
    frame = _entry_frame(fresh_chat=False, conversation=conv, provider=provider)
    with _entry_deps(tmp_path) as deps, mock.patch(
        "codey.operations.kernel_transport.provider_uses_native", return_value=False,
    ):
        run_entry_kernel(frame, _entry_work(), _entry_hooks(), deps, task_kind="chat")
    assert provider.calls == ["send"], f"继续会话只发送任务：{provider.calls}"
    assert conv.estimated_context_tokens >= 12000, f"累计预算必须保留：{conv.estimated_context_tokens}"
    assert conv.snapshot.conversation_summary != "", "原有摘要不得被清空"


def test_entry_kernel_fresh_chat_failure_stops_without_send(tmp_path: Path) -> None:
    from unittest import mock

    from codey.operations.task_entry import run_entry_kernel

    conv = _conversation()
    provider = _TextProvider(fail_new_chat=True)
    frame = _entry_frame(fresh_chat=True, conversation=conv, provider=provider)
    with _entry_deps(tmp_path) as deps, mock.patch(
        "codey.operations.kernel_transport.provider_uses_native", return_value=False,
    ):
        outcome = run_entry_kernel(
            frame, _entry_work(), _entry_hooks(), deps, task_kind="chat",
        )
    assert provider.calls == ["new_chat"], f"失败后不得发送：{provider.calls}"
    assert outcome.event.get("stop_reason") == "provider_failure"
    assert conv.estimated_context_tokens == 12000, "失败不得清空原预算"


def test_nested_provider_adapters_select_protocol_from_actual_provider() -> None:
    from unittest import mock

    from codey.operations.kernel_transport import provider_uses_native
    from codey.operations.provider_session import ConversationProvider, ObservedProvider

    provider = mock.Mock()
    def observe(*_args: object) -> None:
        pass

    wrapped = ObservedProvider(ConversationProvider(provider, mock.Mock()), observe)
    with mock.patch("codey.providers.native_tools.supports_native_tools", return_value=False) as supports:
        assert not provider_uses_native(wrapped, provider_id="local")
    supports.assert_called_once_with(provider, "local")
