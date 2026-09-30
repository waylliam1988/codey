"""会话生命周期必须真实一致：新会话成功才清空窗口，继续会话保留预算。

- fresh_chat 且 new_chat 成功 → begin_window 后再首发；
- fresh_chat 但 new_chat 失败 → 停止并报 provider failure，不向旧会话发送；
- 非 fresh_chat → 保留原窗口与累计预算，不得清空。
"""

from __future__ import annotations


def _conversation():
    from codey.agents.handoff import ConversationContext

    conv = ConversationContext()
    conv.used_tokens = 12000
    return conv


def test_native_exchange_accounts_for_tool_arguments():
    from types import SimpleNamespace

    from codey.operations.provider_session import ConversationProvider
    from codey.providers.base import AssistantTurn, ProviderToolCall

    exchanges = []
    turn = AssistantTurn(tool_calls=(ProviderToolCall("id", "edit", {"content": "x" * 3000}),))
    provider = ConversationProvider(
        SimpleNamespace(send_turn=lambda *_: turn),
        SimpleNamespace(record_exchange=lambda *args: exchanges.append(args)),
    )
    assert provider.send_turn("prompt", []) is turn
    assert len(exchanges) == 1
    assert "x" * 3000 in exchanges[0][1]


def test_begin_window_only_after_successful_new_chat():
    conv = _conversation()
    conv.used_tokens = 12000

    calls: list[str] = []

    class _FailingProvider:
        def new_chat(self):
            calls.append("new_chat")
            raise RuntimeError("tab gone")

        def send(self, prompt: str) -> str:
            calls.append("send")
            return "done"

    from codey.operations.task_entry import _open_fresh_session

    outcome = _open_fresh_session(conv, _FailingProvider(), provider_id="local", kind="hybrid")
    assert outcome.ok is False, "新会话打开失败必须停止"
    assert calls == ["new_chat"], f"失败后不得向旧会话发送：{calls}"
    assert conv.used_tokens == 12000, "失败不得清空原预算"


def test_successful_new_chat_resets_window_before_first_send():
    conv = _conversation()

    calls: list[str] = []

    class _GoodProvider:
        def new_chat(self):
            calls.append("new_chat")

    from codey.operations.task_entry import _open_fresh_session

    outcome = _open_fresh_session(conv, _GoodProvider(), provider_id="local", kind="hybrid")
    assert outcome.ok is True
    assert calls == ["new_chat"]
    assert conv.used_tokens == 0
    assert conv.snapshot.conversation_summary == ""


def _entry_frame(*, fresh_chat: bool, conversation, provider) -> object:
    from types import SimpleNamespace

    request = SimpleNamespace(
        session_id="s-life", project="", task="do things", max_turns=1,
        continue_task=False, provider_id="local", intent="chat",
        requested_capabilities=(), strict_research=False,
        sources_open_required=False, project_changes_required=False,
        denied_capabilities=(), model_hint="",
    )
    return SimpleNamespace(
        request=request, run_id="r-life", task_kind="chat",
        provider=provider, provider_id="local", project_text="",
        conversation=conversation, fresh_chat=fresh_chat,
        handoff="", recovered_tool_outcomes=(),
        recovered_tool_result_batch_id="", provider_session_changed=False,
        trace=None, entry_policy=None,
    )


def _entry_deps() -> object:
    from types import SimpleNamespace

    return SimpleNamespace(
        runtime_mutations=None, managed_outputs=None, state=None,
        workspace_revisions=None,
    )


def _entry_hooks() -> object:
    from types import SimpleNamespace

    return SimpleNamespace(on_event=lambda _e: None, on_shell_request=lambda _a: None)


def _entry_work() -> object:
    from types import SimpleNamespace

    from codey.operations.context import RunWork
    from codey.runtime.observe.execution_evidence import ExecutionEvidence

    return RunWork(
        recent_events=[],
        evidence=ExecutionEvidence(workspace_revision=0, workspace_fingerprint=""),
        trace=SimpleNamespace(call=lambda *a, **k: None),
    )


class _TextProvider:
    def __init__(self, *, fail_new_chat: bool = False) -> None:
        self.calls: list[str] = []
        self.fail_new_chat = fail_new_chat

    def new_chat(self) -> None:
        self.calls.append("new_chat")
        if self.fail_new_chat:
            raise RuntimeError("tab gone")

    def send(self, prompt: str) -> str:
        self.calls.append("send")
        return '{"tool": "done", "args": {"summary": "did stuff"}}'


def test_entry_kernel_continuing_run_keeps_budget_and_summary():
    from unittest import mock

    from codey.agents.handoff import ConversationSnapshot
    from codey.operations.task_entry import run_entry_kernel

    conv = _conversation()
    conv.begin_window("local", "chat", "")
    conv.used_tokens = 12000
    conv.snapshot = ConversationSnapshot(
        **{**conv.snapshot.__dict__, "conversation_summary": "prior summary"},
    )
    provider = _TextProvider()
    frame = _entry_frame(fresh_chat=False, conversation=conv, provider=provider)
    with mock.patch(
        "codey.operations.kernel_transport.provider_uses_native", return_value=False,
    ):
        run_entry_kernel(frame, _entry_work(), _entry_hooks(), _entry_deps(), task_kind="chat")
    assert provider.calls == ["send"], f"继续会话只发送任务：{provider.calls}"
    assert conv.used_tokens >= 12000, f"累计预算必须保留：{conv.used_tokens}"
    assert conv.snapshot.conversation_summary != "", "原有摘要不得被清空"


def test_entry_kernel_fresh_chat_failure_stops_without_send():
    from unittest import mock

    from codey.operations.task_entry import run_entry_kernel

    conv = _conversation()
    provider = _TextProvider(fail_new_chat=True)
    frame = _entry_frame(fresh_chat=True, conversation=conv, provider=provider)
    with mock.patch(
        "codey.operations.kernel_transport.provider_uses_native", return_value=False,
    ):
        outcome = run_entry_kernel(
            frame, _entry_work(), _entry_hooks(), _entry_deps(), task_kind="chat",
        )
    assert provider.calls == ["new_chat"], f"失败后不得发送：{provider.calls}"
    assert outcome.event.get("stop_reason") == "provider_failure"
    assert conv.used_tokens == 12000, "失败不得清空原预算"


def test_nested_provider_adapters_select_protocol_from_actual_provider():
    from unittest import mock

    from codey.operations.kernel_transport import provider_uses_native
    from codey.operations.provider_session import ConversationProvider, ObservedProvider

    provider = mock.Mock()
    wrapped = ObservedProvider(ConversationProvider(provider, mock.Mock()), lambda *args: None)
    with mock.patch("codey.providers.native_tools.supports_native_tools", return_value=False) as supports:
        assert not provider_uses_native(wrapped, provider_id="local")
    supports.assert_called_once_with(provider, "local")
