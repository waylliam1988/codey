"""shell 审批必须完整传递原生调用身份并回答原调用。

真实链路：hook → pending → ticket → 批准/拒绝 → provider 收到原 call id
的结果。只检查 DTO 有字段不能证明这条链。
"""

from __future__ import annotations

LONG_CALL_ID = "shell-call-" + "x" * 100


def test_shell_request_preserves_call_id_verbatim_without_truncation():
    from codey.agents.shell_approval import ShellApprovalRequest

    req = ShellApprovalRequest(
        cwd=".", command="echo hi", call_id=LONG_CALL_ID,
        provider_id="local", turn=3, tool_index=1,
    )
    payload = req.to_payload()
    assert payload["call_id"] == LONG_CALL_ID, "原生协议 ID 必须原样保留，不得截断"


def test_shell_request_rejects_overlong_call_id_without_use():
    from codey.agents.shell_approval import ShellApprovalRequest, valid_shell_call_id

    bad = "c" * 300
    assert valid_shell_call_id(bad) is False
    req = ShellApprovalRequest(cwd=".", command="echo hi", call_id=bad)
    payload = req.to_payload()
    assert payload.get("call_id", "") != bad[:80], "超限 ID 不得截断后使用"
    assert "call_id" not in payload


def test_approval_stop_carries_exact_call_id_turn_and_index():
    import tempfile
    from pathlib import Path

    from codey.operations.task_loop import _approval_stop
    from codey.policies.task_policy import TaskPolicy
    from codey.runtime.core.models import ToolCall

    seen: list[object] = []
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
        proj = Path(td)
        (proj / "a.py").write_text("x", encoding="utf-8")
        calls = [ToolCall(name="shell", args={"command": "echo hi", "path": "."}, call_id=LONG_CALL_ID)]
        policy = TaskPolicy(grants=frozenset({
            "control", "project.read", "project.write", "project.verify", "shell.approval",
        }))
        res = _approval_stop(
            calls, project_path=proj, on_shell_request=seen.append, turn=7,
            run_id="r-shell", intent_sink=None, policy=policy, provider_id="local",
        )
        assert res is not None and res.stop_reason == "approval"
        req = seen[0]
        assert getattr(req, "call_id", "") == LONG_CALL_ID
        assert getattr(req, "provider_id", "") == "local"
        assert int(getattr(req, "turn", -1)) == 7
        assert int(getattr(req, "tool_index", -1)) == 0


def test_approval_pending_persists_full_native_identity():
    from codey.agents.shell_approval import (
        ShellApprovalRequest,
        build_shell_approval_pending,
        shell_command_payload,
    )

    req = ShellApprovalRequest(
        cwd=".", command="echo hi", call_id=LONG_CALL_ID,
        provider_id="local", turn=7, tool_index=0,
    )
    pending = build_shell_approval_pending(
        approval=req, approval_id="shell_test123", session_id="s1", run_id="r1",
        project="E:\\codey", max_turns=10, provider_label="local",
        command_fields=dict(shell_command_payload("echo hi")),
        risk_label="generic", risk_title="t", risk_detail="d",
        post_approval_instructions="",
    )
    assert pending["call_id"] == LONG_CALL_ID
    assert pending["provider_id"] == "local"
    assert pending["turn"] == 7
    assert pending["tool_index"] == 0
    assert pending["run_id"] == "r1"
    assert pending["session_id"] == "s1"


def test_approved_shell_result_answers_original_call_id_natively():
    from unittest import mock

    from codey.agents.shell_approval import (
        ShellApprovalRequest,
        build_shell_approval_pending,
        shell_command_payload,
    )
    from codey.app import shell_service
    from codey.operations.task_loop import run_task_kernel
    from codey.operations.task_session import TaskSession
    from codey.policies.task_policy import TaskPolicy
    from codey.providers.base import AssistantTurn, ProviderToolCall

    req = ShellApprovalRequest(
        cwd=".", command="echo hi", call_id="shell-native-9",
        provider_id="local", turn=2, tool_index=0,
    )
    pending = build_shell_approval_pending(
        approval=req, approval_id="shell_test456", session_id="s-shell", run_id="r-prev",
        project=".", max_turns=4, provider_label="local",
        command_fields=dict(shell_command_payload("echo hi")),
        risk_label="generic", risk_title="t", risk_detail="d",
        post_approval_instructions="",
    )
    approved = {"ok": True, "status": "exit", "output": "hi\n", "exit_code": 0, "truncated": False}
    row = shell_service.shell_result_row(pending, approved, approved=True)
    assert row.call.call_id == "shell-native-9", "必须回答原 native 调用"
    assert row.redelivered is True
    assert row.turn == 0 and row.tool_index == 0

    # 同一 native 会话继续：首条发送即回答原 shell 调用，不执行新工具
    from types import SimpleNamespace

    from codey.operations.recovery import delivered_from_frame

    frame = SimpleNamespace(
        recovered_tool_outcomes=(row,), run_id="r-next",
    )
    delivered = delivered_from_frame(frame, effect_scope="task")
    assert len(delivered) == 1
    initial = list(delivered.values())
    assert initial[0].call.call_id == "shell-native-9"

    executed: list[str] = []

    class P:
        def __init__(self) -> None:
            self.answers = 0

        def send_turn(self, prompt, tools):
            raise AssertionError("native continuation must answer the shell call first")

        def send_tool_results(self, messages, tools):
            self.answers += 1
            if self.answers == 1:
                ids = [m["tool_call_id"] for m in messages]
                assert ids == ["shell-native-9"], f"首发必须回答原调用：{ids}"
                executed.append("answered")
                return AssistantTurn(
                    text="",
                    tool_calls=(ProviderToolCall(id="done-1", name="done", arguments={"summary": "continued"}),),
                )
            return AssistantTurn(text="ack")

    session = TaskSession(
        policy=TaskPolicy(grants=frozenset({"control", "project.read"})),
        task_kind="project", project=".", max_turns=3,
    )
    with mock.patch(
        "codey.operations.kernel_transport.provider_uses_native", return_value=True,
    ):
        out = run_task_kernel(
            session, provider=P(), run_id="r-next", effect_scope="task",
            provider_id="local", user_task="continue",
            delivered=delivered, initial_results=initial,
        )
    assert executed == ["answered"]
    assert out.stop_reason == "done"


def test_denied_shell_result_answers_original_call_id():
    from codey.agents.shell_approval import (
        ShellApprovalRequest,
        build_shell_approval_pending,
        shell_command_payload,
    )
    from codey.app import shell_service

    req = ShellApprovalRequest(
        cwd=".", command="rm -rf /", call_id="shell-deny-3", provider_id="local",
    )
    pending = build_shell_approval_pending(
        approval=req, approval_id="shell_test789", session_id="s1", run_id="r1",
        project=".", max_turns=4, provider_label="local",
        command_fields=dict(shell_command_payload("rm -rf /")),
        risk_label="generic", risk_title="t", risk_detail="d",
        post_approval_instructions="",
    )
    denied = {"ok": False, "status": "denied", "output": "Denied by user.", "exit_code": None}
    row = shell_service.shell_result_row(pending, denied, approved=False)
    assert row.call.call_id == "shell-deny-3"
    assert row.redelivered is True
    assert "Denied by user" in str(row.outcome.model_text)


def test_call_id_with_spaces_is_preserved_as_an_identity():
    from codey.agents.shell_approval import ShellApprovalRequest

    call_id = " native identity "
    assert ShellApprovalRequest(".", "echo hi", call_id=call_id).to_payload()["call_id"] == call_id


def test_session_projection_never_shortens_native_call_id():
    from codey.operations.task_session import TaskSession
    from codey.policies.task_policy import TaskPolicy

    session = TaskSession(policy=TaskPolicy(grants=frozenset({"control"})))
    session.executed["effect"] = {"name":"run", "call_id":LONG_CALL_ID, "ok":True}
    restored = TaskSession.from_payload(session.to_payload(), policy=session.policy)
    assert restored.executed["effect"]["call_id"] == LONG_CALL_ID


def test_malformed_shell_result_is_rejected_instead_of_coercing_success():
    import pytest

    from codey.operations.kernel_errors import RecoveryFailed
    from codey.operations.task_run import _shell_redelivery_rows

    with pytest.raises(RecoveryFailed):
        _shell_redelivery_rows(({"call_id":"original", "command":"echo hi", "cwd":".",
                                  "model_text":"failed", "ok":"false", "exit_code":0},))
