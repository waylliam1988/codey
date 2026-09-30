"""原生协议所有终止路径必须先回答当前 call id 再结束。"""
from __future__ import annotations

from types import SimpleNamespace


def _native_reply(*ids: str):
    return SimpleNamespace(tool_calls=tuple(SimpleNamespace(id=i, name="read_file", arguments={"path": "a.py"}) for i in ids))


def test_protocol_threshold_closes_current_call_id() -> None:
    from unittest import mock

    from codey.operations.task_loop import run_task_kernel
    from codey.operations.task_session import TaskSession
    from codey.policies.task_policy import TaskPolicy

    policy = TaskPolicy(grants=frozenset({"control", "project.read"}))
    sess = TaskSession(policy=policy, task_kind="project", project="", max_turns=4, task_text="t")
    closed: list[str] = []

    class P:
        def send_turn(self, prompt, tools):
            return SimpleNamespace(tool_calls=(
                SimpleNamespace(id="invalid-last-id", name="unknown_tool_xyz", arguments={}),
            ))
        def send_tool_results(self, messages, tools):
            closed.extend(m["tool_call_id"] for m in messages)
            return SimpleNamespace(tool_calls=())

    with mock.patch("codey.operations.kernel_transport.provider_uses_native", return_value=True):
        out = run_task_kernel(sess, provider=P(), run_id="r-proto", effect_scope="task",
                              provider_id="x", user_task="t", stagnant_turns=1)
    assert out.stop_reason == "protocol", f"应以 protocol 退出，实际 {out.stop_reason}"
    assert "invalid-last-id" in closed, f"阈值退出前必须先回答当前 id，实际已回答 {closed}"


def test_shell_approval_carries_native_call_id() -> None:
    from codey.operations.task_loop import _approval_stop
    from codey.runtime.core.models import ToolCall

    seen: list[object] = []

    class _Flag:
        def is_set(self):
            return False

    import tempfile
    from pathlib import Path
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
        proj = Path(td)
        (proj / "a.py").write_text("x", encoding="utf-8")
        calls = [ToolCall(name="shell", args={"command": "echo hi", "path": "."}, call_id="shell-1")]
        from codey.policies.task_policy import TaskPolicy
        policy = TaskPolicy(grants=frozenset({"control", "project.read", "project.write", "project.verify", "shell.approval"}))
        res = _approval_stop(calls, project_path=proj, on_shell_request=seen.append, turn=1,
                             run_id="r1", intent_sink=None, policy=policy)
        assert res is not None and res.stop_reason == "approval"
        req = seen[0]
        # 精确身份：原 call id 原样保留（不得截断），provider 与轮次一并携带
        assert getattr(req, "call_id", "") == "shell-1"
        payload = req.to_payload() if hasattr(req, "to_payload") else {}
        assert payload.get("call_id") == "shell-1"
