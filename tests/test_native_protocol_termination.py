"""原生协议错误终止必须关闭后续 call id，不只回答当前回复。

模型返回非法调用达到阈值后，内核退出前必须有界关闭整条链：
当前 id 与 provider 后续返回的新调用都要被回答，且不执行任何工具。
"""

from __future__ import annotations

from unittest import mock

from codey.operations.task_loop import (
    KernelExecutionDeps,
    KernelRunRequest,
    KernelTransportDeps,
    run_task_kernel,
)
from codey.operations.task_session import TaskSession
from codey.policies.task_policy import TaskPolicy
from codey.providers.base import AssistantTurn, ProviderToolCall


class _ThresholdProvider:
    name = "local"

    def __init__(self) -> None:
        self.receipt_ids: list[str] = []
        self.calls = 0
        self._replies: list[AssistantTurn] = [
            AssistantTurn(
                text="",
                tool_calls=(ProviderToolCall(id="bad-1", name="nope_unknown_tool", arguments={}),),
            ),
            AssistantTurn(
                text="",
                tool_calls=(ProviderToolCall(id="followup-2", name="nope_other_tool", arguments={}),),
            ),
            AssistantTurn(text="ack"),
        ]

    def send_turn(self, prompt: str, tools=None, timeout=None) -> AssistantTurn:
        return self._replies.pop(0)

    def send_tool_results(self, results, tools=None, timeout=None) -> AssistantTurn:
        for row in list(results or ()):
            self.receipt_ids.append(str(row.call_id))
        return self._replies.pop(0)

    def acknowledge_tool_results(self, results, declared_tools, timeout=None):
        return self.send_tool_results(results, [])


def test_protocol_threshold_close_answers_followup_call_ids_without_executing():
    executed: list[str] = []
    session = TaskSession(
        policy=TaskPolicy(grants=frozenset({"control", "knowledge.read"})),
        task_kind="research", max_turns=4,
    )
    provider = _ThresholdProvider()
    with mock.patch(
        "codey.operations.kernel_transport.provider_uses_native", return_value=True,
    ):
        result = run_task_kernel(
            session,
            request=KernelRunRequest(
                transport=KernelTransportDeps(
                    provider=provider,
                    run_id="run-proto-close",
                    provider_id="local",
                    user_task="q",
                    stagnant_turns=1,
                ),
                execution=KernelExecutionDeps(
                    executors={"knowledge_search": lambda call: executed.append(call.name) or "x"},
                ),
            ),
        )
    assert result.stop_reason == "protocol", f"阈值应终止为 protocol：{result}"
    assert executed == [], f"终止路径不得执行后续工具：{executed}"
    assert "bad-1" in provider.receipt_ids, f"当前 id 必须被回答：{provider.receipt_ids}"
    assert "followup-2" in provider.receipt_ids, (
        f"后续新调用也必须被回答，不得遗漏：{provider.receipt_ids}"
    )
