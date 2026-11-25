"""shell 审批必须完整传递原生调用身份并回答原调用。

真实链路：hook → pending → ticket → 批准/拒绝 → provider 收到原 call id
的结果。只检查 DTO 有字段不能证明这条链。
"""

from __future__ import annotations

from collections.abc import Generator, Mapping
from contextlib import AbstractContextManager, contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol, runtime_checkable

import pytest

from codey.agents.shell_approval import ShellApprovalRequest
from codey.durable import DurableSession
from codey.operations.kernel_contracts import KernelRunRequest
from codey.operations.task_effects import KernelEffectSink
from codey.providers.base import ProviderToolResult
from codey.storage.managed_outputs import ManagedOutputStore
from codey.utils.object_fields import object_fields
from codey.workspace.revision import WorkspaceRevisionStore


@runtime_checkable
class _ApprovedShellFactory(Protocol):
    def __call__(self, root: Path, receipt: Mapping[str, object], *,
                 call_id: str = "original-shell") -> AbstractContextManager[object]: ...


@dataclass(frozen=True)
class _ShellRuntime:
    durable: DurableSession
    outputs: ManagedOutputStore
    request: KernelRunRequest
    events: list[object]


@contextmanager
def _approved_shell(root: Path, receipt: Mapping[str, object], *,
                    call_id: str = "original-shell") -> Generator[_ShellRuntime, None, None]:
    from tests.support import kernel_restart

    factory: object = kernel_restart.approved_shell
    assert isinstance(factory, _ApprovedShellFactory)
    with factory(root, receipt, call_id=call_id) as raw:
        durable: object = getattr(raw, "durable", None)
        outputs: object = getattr(raw, "outputs", None)
        request: object = getattr(raw, "request", None)
        events: object = getattr(raw, "events", None)
        assert isinstance(durable, DurableSession)
        assert isinstance(outputs, ManagedOutputStore)
        assert isinstance(request, KernelRunRequest)
        assert isinstance(events, list)
        yield _ShellRuntime(durable, outputs, request, events)


@runtime_checkable
class _OpenRuntime(Protocol):
    def __call__(self, logdir: Path, state: Path, session_id: str,
                 run_id: str, project: Path) -> object: ...


@runtime_checkable
class _RecoverRuntime(Protocol):
    def __call__(self, project: Path, state: Path, logdir: Path,
                 session_id: str, run_id: str) -> object: ...

LONG_CALL_ID = "shell-call-" + "x" * 100


def test_shell_request_preserves_call_id_verbatim_without_truncation() -> None:
    from codey.agents.shell_approval import ShellApprovalRequest

    req = ShellApprovalRequest(
        cwd=".", command="echo hi", call_id=LONG_CALL_ID,
        provider_id="local", turn=3, tool_index=1,
    )
    payload = req.to_payload()
    assert payload["call_id"] == LONG_CALL_ID, "原生协议 ID 必须原样保留，不得截断"


def test_shell_request_rejects_overlong_call_id_without_use() -> None:
    from codey.agents.shell_approval import ShellApprovalRequest, valid_shell_call_id

    bad = "c" * 300
    assert valid_shell_call_id(bad) is False
    req = ShellApprovalRequest(cwd=".", command="echo hi", call_id=bad)
    payload = req.to_payload()
    assert payload.get("call_id", "") != bad[:80], "超限 ID 不得截断后使用"
    assert "call_id" not in payload


def test_approval_stop_carries_exact_call_id_turn_and_index() -> None:
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
        assert isinstance(req, ShellApprovalRequest)
        assert req.call_id == LONG_CALL_ID
        assert req.provider_id == "local"
        assert req.turn == 7
        assert req.tool_index == 0


def test_approval_pending_persists_full_native_identity() -> None:
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


def test_approved_shell_result_answers_original_call_id_natively(tmp_path: Path) -> None:
    from codey.operations.task_loop import run_task_kernel

    receipt = {"ok": True, "status": "exit", "output": "hi", "exit_code": 0, "truncated": False}
    with _approved_shell(tmp_path, receipt, call_id=LONG_CALL_ID) as runtime:
        from codey.operations.task_session import TaskSession
        from codey.policies.task_policy import TaskPolicy

        session = TaskSession(TaskPolicy(frozenset({
            "control", "project.read", "project.verify", "shell.approval",
        })), max_turns=4)
        result = run_task_kernel(session, request=runtime.request)
        assert result.completed, result
        sent: list[ProviderToolResult] = []
        for event in runtime.events:
            assert isinstance(event, tuple) and len(event) == 2
            kind, rows = tuple[object, ...](event)
            if kind == "results":
                assert isinstance(rows, list)
                for row in list[object](rows):
                    assert isinstance(row, ProviderToolResult)
                    sent.append(row)
        assert len(sent) == 1 and sent[0].call_id == LONG_CALL_ID
        assert "hi" in sent[0].content


def test_denied_shell_result_answers_original_call_id() -> None:
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
    assert row is not None
    assert row.call.call_id == "shell-deny-3"
    assert row.redelivered is True
    assert "Denied by user" in str(row.outcome.model_text)


def test_call_id_with_spaces_is_preserved_as_an_identity() -> None:
    from codey.agents.shell_approval import ShellApprovalRequest

    call_id = " native identity "
    assert ShellApprovalRequest(".", "echo hi", call_id=call_id).to_payload()["call_id"] == call_id


def test_session_projection_never_shortens_native_call_id() -> None:
    import tempfile
    from pathlib import Path

    from codey.operations.kernel.kernel_execution import execute_turn
    from codey.operations.task_session import TaskSession
    from codey.operations.turn_snapshot import build_turn_snapshot
    from codey.policies.task_policy import TaskPolicy
    from codey.runtime.core.models import ToolCall, ToolResult
    from codey.runtime.observe.execution_evidence import ExecutionEvidence

    policy = TaskPolicy(grants=frozenset({"control", "project.read", "project.write", "project.verify"}))

    # In-memory: real execute_turn keeps the long native call id verbatim.
    session = TaskSession(policy=policy)
    def _fake_run(call: ToolCall) -> ToolResult:
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
        open_runtime: object = _open_runtime
        assert isinstance(open_runtime, _OpenRuntime)
        opened = open_runtime(logdir, state, session_id, run_id, project)
        assert isinstance(opened, tuple) and len(opened) == 6
        fields = tuple[object, ...](opened)
        rev_store, sink = fields[3], fields[5]
        assert isinstance(rev_store, WorkspaceRevisionStore)
        assert isinstance(sink, KernelEffectSink)
        (project / "a.py").write_text("x = 1\n", encoding="utf-8")
        logged = TaskSession(policy=policy, task_kind="project", project=str(project), max_turns=8)
        base = rev_store.current_state(str(project), ignored_paths=())
        evidence = ExecutionEvidence(
            workspace_revision=int(base.revision or 0),
            workspace_fingerprint=base.fingerprint,
        )
        def _fake_edit(call: ToolCall) -> ToolResult:
            (project / str(call.args.get("path") or "a.py")).write_text(
                str(call.args.get("content") or "x = 1\n"), encoding="utf-8"
            )
            return ToolResult(ok=True, call=call, model_text="edited", audit={"changed": True})
        def _fake_logged_run(call: ToolCall) -> ToolResult:
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
        recover_runtime: object = _recover_formal
        assert isinstance(recover_runtime, _RecoverRuntime)
        recovered = recover_runtime(project, state, logdir, session_id, run_id)
        assert isinstance(recovered, tuple) and len(recovered) == 5
        recovery = tuple[object, ...](recovered)[1]
        recovered_ok: object = getattr(recovery, "ok", None)
        assert recovered_ok is True
        # The canonical session owns provider tool history. Recovery rows are
        # execution projections and may be empty once the delivery is settled;
        # verify the durable protocol fact directly instead of reconstructing
        # it from the legacy runtime log.
        from codey.durable import DurableSession, SQLiteDurableStore
        from codey.durable.model import thaw_json
        durable = DurableSession(SQLiteDurableStore(state / "durable.sqlite3"))
        try:
            effects = durable.effects(run_id)
            intents = [thaw_json(effect.intent) for effect in effects]
            assert any(object_fields(intent) and intent.get("call_id") == LONG_CALL_ID for intent in intents)
        finally:
            durable.close()


def test_malformed_shell_result_is_rejected_instead_of_coercing_success(tmp_path: Path) -> None:

    from codey.operations.shell_approval_receipt import resolve_shell_approval

    receipt = {"ok": "false", "status": "exit", "output": "failed", "exit_code": 0}
    with _approved_shell(tmp_path, receipt) as runtime, pytest.raises(ValueError, match="ok must be a boolean"):
        resolve_shell_approval(runtime.durable, runtime.outputs, "approval")
