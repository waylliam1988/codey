"""native done 必须经真实 kernel 收口后续调用（非仅 _take_answered_reply）。"""
from __future__ import annotations

from types import SimpleNamespace

from codey.operations.task_loop import KernelExecutionDeps, KernelRunRequest, KernelTransportDeps
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
        self.answered.append([m.call_id for m in messages])
        self.results_calls += 1
        if self.results_calls == 1:
            # 回答 done-1 后，模型又提出后续调用 followup-1，必须被收口关闭且不执行
            return SimpleNamespace(
                tool_calls=(SimpleNamespace(id="followup-1", name="read_file", arguments={"path": "a.py"}),),
            )
        return SimpleNamespace(tool_calls=())

    def acknowledge_tool_results(self, results, declared_tools, timeout=None):
        return self.send_tool_results(results, [])


def test_done_receipt_closes_followup_via_real_kernel(tmp_path) -> None:
    from unittest import mock

    from codey.operations.task_loop import run_task_kernel

    (tmp_path / "a.py").write_text("x\n", encoding="utf-8")
    provider = _SeqNativeProvider()
    executed: list[str] = []
    # 后续工具有权限也有执行器：收口必须保证执行次数为 0（而不靠无权限挡掉）
    sess2 = TaskSession(
        policy=TaskPolicy(grants=frozenset({"control", "project.read"})),
        task_kind="project", project=str(tmp_path), max_turns=4, task_text="q",
    )
    with mock.patch("codey.operations.kernel_transport.provider_uses_native", return_value=True):
        out = run_task_kernel(
            sess2,
            request=KernelRunRequest(
                transport=KernelTransportDeps(
                    provider=provider,
                    run_id="r-done",
                    effect_scope="task",
                    provider_id="test",
                    user_task="q",
                ),
                execution=KernelExecutionDeps(
                    project_path=tmp_path,
                    executors={"read_file": lambda call: executed.append(call.name) or "x"},
                ),
            ),
        )
    flat = [i for batch in provider.answered for i in batch]
    assert "done-1" in flat, f"done id 未被回答: {provider.answered}"
    assert "followup-1" in flat, f"后续调用未被收口关闭: {provider.answered}"
    assert executed == [], f"已完成任务的后续调用不得执行：{executed}"
    assert out.completed
