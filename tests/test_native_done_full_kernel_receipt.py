"""native done 必须经真实 kernel 收口后续调用（非仅 _take_answered_reply）。"""
from __future__ import annotations

from types import SimpleNamespace

from codey.operations.task_session import TaskSession
from codey.policies.task_policy import TaskPolicy


class _SeqNativeProvider:
    """首轮返回 done；回答 done 后再返回另一个工具调用，随后关闭。"""

    def __init__(self):
        self.turn = 0
        self.results_calls = 0
        self.answered: list[list[str]] = []
        self.executed: list[str] = []

    def send_turn(self, prompt, tools):
        self.turn += 1
        return SimpleNamespace(
            tool_calls=(SimpleNamespace(id="done-1", name="done", arguments={"summary": "hello"}),),
        )

    def send_tool_results(self, messages, tools):
        self.answered.append([m["tool_call_id"] for m in messages])
        self.results_calls += 1
        if self.results_calls == 1:
            # 回答 done-1 后，模型又提出后续调用 followup-1，必须被收口关闭且不执行
            return SimpleNamespace(
                tool_calls=(SimpleNamespace(id="followup-1", name="read_file", arguments={"path": "a.py"}),),
            )
        return SimpleNamespace(tool_calls=())


def test_done_receipt_closes_followup_via_real_kernel() -> None:
    from unittest import mock

    from codey.operations.task_loop import run_task_kernel

    provider = _SeqNativeProvider()
    sess2 = TaskSession(policy=TaskPolicy(grants=frozenset({"control"})), task_kind="chat",
                        project="", max_turns=4, task_text="q")
    with mock.patch("codey.operations.kernel_transport.provider_uses_native", return_value=True):
        out = run_task_kernel(sess2, provider=provider, run_id="r-done", effect_scope="task",
                              provider_id="test", user_task="q")
    flat = [i for batch in provider.answered for i in batch]
    assert "done-1" in flat, f"done id 未被回答: {provider.answered}"
    assert "followup-1" in flat, f"后续调用未被收口关闭: {provider.answered}"
    assert out.completed
