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

    from codey.operations.kernel_actions import _approval_stop
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
        project="E:\\codey", max_turns=10, provider_label="local", attached_project="E:/codey", intent="project",
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


def test_approved_shell_result_answers_original_call_id_natively(tmp_path):
    from codey.operations.task_loop import run_task_kernel
    from tests.support.kernel_restart import approved_shell

    receipt = {"ok": True, "status": "exit", "output": "hi", "exit_code": 0, "truncated": False}
    with approved_shell(tmp_path, receipt, call_id=LONG_CALL_ID) as runtime:
        result = run_task_kernel(runtime.session(), request=runtime.request)
        assert result.completed, result
        sent = [row for kind, rows in runtime.events if kind == "results" for row in rows]
        assert len(sent) == 1 and sent[0].call_id == LONG_CALL_ID
        assert "hi" in sent[0].content


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
        project=".", max_turns=4, provider_label="local", attached_project=".", intent="project",
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
    import tempfile
    from pathlib import Path

    from codey.operations.kernel_execution import execute_turn
    from codey.operations.kernel_protocol import build_turn_snapshot
    from codey.operations.task_session import TaskSession
    from codey.policies.task_policy import TaskPolicy
    from codey.runtime.core.models import ToolCall, ToolResult
    from codey.runtime.observe.execution_evidence import ExecutionEvidence

    policy = TaskPolicy(grants=frozenset({"control", "project.read", "project.write", "project.verify"}))

    # In-memory: real execute_turn keeps the long native call id verbatim.
    session = TaskSession(policy=policy)
    def _fake_run(call):
        return ToolResult(ok=True, call=call, model_text="ok", audit={"exit_code": 0})
    calls = [ToolCall(name="run", args={"command": "echo hi", "path": "."}, call_id=LONG_CALL_ID)]
    results = execute_turn(session, calls, executors={"run": _fake_run}, run_id="r-long", turn=1)
    assert results[0].call.call_id == LONG_CALL_ID
    assert any(row.get("call_id") == LONG_CALL_ID for row in session.executed.values())

    # Real durable recovery via the formal entry preserves the native call id.
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
        tmp = Path(td)
        project = tmp / "project"
        state = tmp / "state"
        logdir = tmp / "log"
        project.mkdir(parents=True, exist_ok=True)
        state.mkdir(parents=True, exist_ok=True)
        logdir.mkdir(parents=True, exist_ok=True)
        from tests.test_session_log_receipt_recovery_preserves_facts import (
            _open_runtime,
            _recover_formal,
        )

        session_id = "s-long"
        run_id = "r-long-log"
        _, _, _, rev_store, _, sink = _open_runtime(
            logdir, state, session_id, run_id, project,
        )
        (project / "a.py").write_text("x = 1\n", encoding="utf-8")
        logged = TaskSession(policy=policy, task_kind="project", project=str(project), max_turns=8)
        base = rev_store.current_state(str(project), ignored_paths=())
        evidence = ExecutionEvidence(
            workspace_revision=int(base.revision or 0),
            workspace_fingerprint=base.fingerprint,
        )
        def _fake_edit(call):
            (project / str(call.args.get("path") or "a.py")).write_text(
                str(call.args.get("content") or "x = 1\n"), encoding="utf-8"
            )
            return ToolResult(ok=True, call=call, model_text="edited", audit={"changed": True})
        def _fake_logged_run(call):
            return ToolResult(ok=True, call=call, model_text="run out", audit={"exit_code": 0})
        edit_call = ToolCall(name="edit", args={"path": "a.py", "content": "x = 1\n"})
        run_call = ToolCall(name="run", args={"command": "echo hi", "path": "."}, call_id=LONG_CALL_ID)
        snapshot = build_turn_snapshot(logged)
        logged_results = execute_turn(
            logged, [edit_call, run_call],
            executors={"edit": _fake_edit, "run": _fake_logged_run},
            run_id=run_id, effect_scope="task", turn=1,
            project_path=project, intent_sink=sink, snapshot=snapshot,
            execution_evidence=evidence, workspace_revision_store=rev_store,
        )
        assert len(logged_results) == 2
        assert logged_results[1].call.call_id == LONG_CALL_ID

        sink.durable.close()
        fresh, recovery, delivered, rows, _ = _recover_formal(
            project, state, logdir, session_id, run_id,
        )
        assert recovery.ok is True
        # The canonical session owns provider tool history. Recovery rows are
        # execution projections and may be empty once the delivery is settled;
        # verify the durable protocol fact directly instead of reconstructing
        # it from the legacy runtime log.
        from codey.durable import DurableSession, SQLiteDurableStore
        from codey.durable.model import thaw_json
        durable = DurableSession(SQLiteDurableStore(state / "durable.sqlite3"))
        try:
            effects = durable.effects(run_id)
            assert any(
                thaw_json(effect.intent).get("call_id") == LONG_CALL_ID
                for effect in effects
            )
        finally:
            durable.close()


def test_malformed_shell_result_is_rejected_instead_of_coercing_success(tmp_path):
    import pytest

    from codey.operations.shell_approval_receipt import resolve_shell_approval
    from tests.support.kernel_restart import approved_shell

    receipt = {"ok": "false", "status": "exit", "output": "failed", "exit_code": 0}
    with approved_shell(tmp_path, receipt) as runtime, pytest.raises(ValueError, match="receipt fields"):
        resolve_shell_approval(runtime.durable, runtime.outputs, "approval")
