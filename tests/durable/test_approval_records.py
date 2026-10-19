from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

from codey.durable import (
    ApprovalRecord,
    ApprovalStatus,
    CommitConflict,
    DurableSession,
    MemoryDurableStore,
    SQLiteDurableStore,
    TaskStatus,
)
from codey.durable.model import EffectRecord


def test_shell_approval_admission_is_durable_before_audit_projection(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The JSONL audit projection must never own approval admission."""
    from codey.app.context import AppContext

    state = AppContext(tmp_path)
    try:
        state.durable.admit_input(
            conversation_id="conversation-1",
            request_id="request-1",
            task_id="task-1",
            text="run a shell command",
            provider_snapshot={"provider_id": "local"},
        )
        # A shell approval is an effect of an active task.  The canonical
        # fixture must enter the same durable phase as production before it
        # can claim an external effect.
        state.durable.start_task("task-1", state={})
        state.durable.advance_task(
            task_id="task-1", phase="assistant_response_pending", state={}
        )
        artifact = state.managed_outputs.write_json_artifact(
            session_id="conversation-1",
            run_id="task-1",
            payload={"command": "echo hello", "cwd": "."},
        )

        def projection_failure(_fact: object) -> None:
            raise OSError("audit projection unavailable")

        monkeypatch.setattr(state.shell_approval_records, "append", projection_failure)
        state.add_pending_shell_approval(
            "approval-1",
            {
                "session_id": "conversation-1",
                "run_id": "task-1",
                "command": "echo hello",
                "command_ref": artifact.handle,
                "command_digest": artifact.sha256,
                "cwd": ".",
            },
        )

        approval = state.durable.approval("approval-1")
        assert approval.status == ApprovalStatus.REQUESTED
        assert approval.task_id == "task-1"
        assert state.approvals.shell_snapshot()["approval-1"]["command"] == "echo hello"
    finally:
        state.close()


def test_shell_approval_claim_uses_durable_state_when_audit_projection_is_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from codey.app.context import AppContext

    state = AppContext(tmp_path)
    try:
        state.durable.admit_input(
            conversation_id="conversation-1",
            request_id="request-1",
            task_id="task-1",
            text="run a shell command",
            provider_snapshot={"provider_id": "local"},
        )
        state.durable.start_task("task-1", state={})
        state.durable.advance_task(
            task_id="task-1", phase="assistant_response_pending", state={}
        )
        artifact = state.managed_outputs.write_json_artifact(
            session_id="conversation-1",
            run_id="task-1",
            payload={"command": "echo hello", "cwd": "."},
        )
        monkeypatch.setattr(
            state.shell_approval_records,
            "append",
            lambda _fact: (_ for _ in ()).throw(OSError("audit projection unavailable")),
        )
        state.add_pending_shell_approval(
            "approval-1",
            {
                "session_id": "conversation-1",
                "run_id": "task-1",
                "command": "echo hello",
                "command_ref": artifact.handle,
                "command_digest": artifact.sha256,
                "cwd": ".",
            },
        )
        from codey.runtime.effects.shell_approval_records import ShellApprovalFact

        state.record_shell_approval_fact(ShellApprovalFact(
            approval_id="approval-1",
            session_id="conversation-1",
            source_run_id="task-1",
            kind="approved",
            command_ref=artifact.handle,
            command_digest=artifact.sha256,
            cwd=".",
        ))
        claimed = state.claim_pending_shell_approval("approval-1")
        assert claimed is not None
        assert state.durable.approval("approval-1").status == ApprovalStatus.EXECUTION_CLAIMED
    finally:
        state.close()


def test_shell_approval_status_reads_durable_record_without_audit_projection(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from codey.app.context import AppContext

    state = AppContext(tmp_path)
    try:
        state.durable.admit_input(
            conversation_id="conversation-1",
            request_id="request-1",
            task_id="task-1",
            text="run a shell command",
            provider_snapshot={"provider_id": "local"},
        )
        artifact = state.managed_outputs.write_json_artifact(
            session_id="conversation-1",
            run_id="task-1",
            payload={"command": "echo hello", "cwd": "."},
        )
        monkeypatch.setattr(
            state.shell_approval_records,
            "append",
            lambda _fact: (_ for _ in ()).throw(OSError("audit projection unavailable")),
        )
        state.add_pending_shell_approval(
            "approval-1",
            {
                "session_id": "conversation-1",
                "run_id": "task-1",
                "command": "echo hello",
                "command_ref": artifact.handle,
                "command_digest": artifact.sha256,
                "cwd": ".",
            },
        )
        status = state.shell_approval_workflow.status("approval-1")
        assert status is not None
        assert status["decision"] == "pending"
    finally:
        state.close()


def test_shell_approval_runtime_never_reads_audit_projection(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The durable approval is sufficient when the JSONL audit is unavailable."""
    from codey.app.context import AppContext

    state = AppContext(tmp_path)
    try:
        state.durable.admit_input(
            conversation_id="conversation-1",
            request_id="request-1",
            task_id="task-1",
            text="run a shell command",
            provider_snapshot={"provider_id": "local"},
        )
        artifact = state.managed_outputs.write_json_artifact(
            session_id="conversation-1",
            run_id="task-1",
            payload={"command": "echo hello", "cwd": "."},
        )
        state.add_pending_shell_approval(
            "approval-1",
            {
                "session_id": "conversation-1",
                "run_id": "task-1",
                "command": "echo hello",
                "command_ref": artifact.handle,
                "command_digest": artifact.sha256,
                "cwd": ".",
            },
        )
        audit = state.shell_approval_records
        monkeypatch.setattr(audit, "read", lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("missing audit")))
        monkeypatch.setattr(audit, "latest", lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("missing audit")))
        monkeypatch.setattr(audit, "histories", lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("missing audit")))

        status = state.shell_approval_workflow.status("approval-1")
        assert status is not None
        assert status["decision"] == "pending"
        assert state.shell_approval_workflow.list_statuses() == [status]

        state.approvals.pop_shell("approval-1")
        from codey.app.shell_approval_state import restore_pending_shell_approvals

        assert restore_pending_shell_approvals(state) == 1
        assert "approval-1" in state.approvals.shell_snapshot()
    finally:
        state.close()


def test_shell_approval_accept_commits_durable_decision_without_audit_projection(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from codey.app.context import AppContext

    state = AppContext(tmp_path)
    try:
        state.durable.admit_input(
            conversation_id="conversation-1",
            request_id="request-1",
            task_id="task-1",
            text="run a shell command",
            provider_snapshot={"provider_id": "local"},
        )
        artifact = state.managed_outputs.write_json_artifact(
            session_id="conversation-1",
            run_id="task-1",
            payload={"command": "echo hello", "cwd": "."},
        )
        monkeypatch.setattr(
            state.shell_approval_records,
            "append",
            lambda _fact: (_ for _ in ()).throw(OSError("audit projection unavailable")),
        )
        state.add_pending_shell_approval(
            "approval-1",
            {
                "session_id": "conversation-1",
                "run_id": "task-1",
                "command": "echo hello",
                "command_ref": artifact.handle,
                "command_digest": artifact.sha256,
                "cwd": ".",
            },
        )
        monkeypatch.setattr(state.shell_approval_workflow, "_schedule", lambda _id: None)
        code, _payload = state.shell_approval_workflow.accept(
            "approval-1", True, lambda *_args, **_kwargs: None,
        )
        assert code == 202
        assert state.durable.approval("approval-1").status == ApprovalStatus.APPROVED
    finally:
        state.close()


def test_settled_shell_approval_resolves_from_durable_state_without_audit_projection(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from codey.app.context import AppContext
    from codey.runtime.effects.shell_approval_records import ShellApprovalFact

    state = AppContext(tmp_path)
    try:
        state.durable.admit_input(
            conversation_id="conversation-1",
            request_id="request-1",
            task_id="task-1",
            text="run a shell command",
            provider_snapshot={"provider_id": "local"},
        )
        command = state.managed_outputs.write_json_artifact(
            session_id="conversation-1",
            run_id="task-1",
            payload={"command": "echo hello", "cwd": ".", "call_id": "call-1"},
        )
        monkeypatch.setattr(
            state.shell_approval_records,
            "append",
            lambda _fact: (_ for _ in ()).throw(OSError("audit projection unavailable")),
        )
        requested = ShellApprovalFact(
            "approval-1", "conversation-1", "task-1", "requested",
            command_ref=command.handle, command_digest=command.sha256, cwd=".",
        )
        state.record_shell_approval_fact(requested)
        state.record_shell_approval_fact(replace(requested, kind="approved"))
        state.record_shell_approval_fact(replace(requested, kind="execution_claimed"))
        result = state.managed_outputs.write_json_artifact(
            session_id="conversation-1",
            run_id="task-1",
            payload={"ok": True, "output": "hello", "exit_code": 0},
        )
        state.record_shell_approval_fact(replace(
            requested,
            kind="execution_settled",
            result_ref=result.handle,
            result_digest=result.sha256,
        ))

        resolved = state.resolve_shell_approval("approval-1")
        assert resolved is not None
        assert resolved["ok"] is True
        assert resolved["model_text"] == "hello"
    finally:
        state.close()


def test_restart_rebuilds_pending_approval_from_task_checkpoint_without_audit_projection(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from codey.app.context import AppContext
    from codey.runtime.effects.shell_approval_records import ShellApprovalFact

    state = AppContext(tmp_path)
    try:
        state.durable.admit_input(
            conversation_id="conversation-1",
            request_id="request-1",
            task_id="task-1",
            text="run a shell command",
            provider_snapshot={"provider_id": "local"},
        )
        command = state.managed_outputs.write_json_artifact(
            session_id="conversation-1",
            run_id="task-1",
            payload={"command": "echo hello", "cwd": "."},
        )
        monkeypatch.setattr(
            state.shell_approval_records,
            "append",
            lambda _fact: (_ for _ in ()).throw(OSError("audit projection unavailable")),
        )
        requested = ShellApprovalFact(
            "approval-1", "conversation-1", "task-1", "requested",
            command_ref=command.handle, command_digest=command.sha256, cwd=".",
        )
        state.record_shell_approval_fact(requested)
        state.record_shell_approval_fact(replace(requested, kind="approved"))
    finally:
        state.close()

    restarted = AppContext(tmp_path)
    try:
        pending = restarted.approvals.shell_snapshot().get("approval-1")
        assert pending is not None
        assert pending["command"] == "echo hello"
    finally:
        restarted.close()


def test_restart_schedules_durable_approved_shell_without_audit_projection(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from codey.app.context import AppContext
    from codey.runtime.effects.shell_approval_records import ShellApprovalFact

    state = AppContext(tmp_path)
    try:
        state.durable.admit_input(
            conversation_id="conversation-1",
            request_id="request-1",
            task_id="task-1",
            text="run a shell command",
            provider_snapshot={"provider_id": "local"},
        )
        command = state.managed_outputs.write_json_artifact(
            session_id="conversation-1",
            run_id="task-1",
            payload={"command": "echo hello", "cwd": "."},
        )
        monkeypatch.setattr(
            state.shell_approval_records,
            "append",
            lambda _fact: (_ for _ in ()).throw(OSError("audit projection unavailable")),
        )
        requested = ShellApprovalFact(
            "approval-1", "conversation-1", "task-1", "requested",
            command_ref=command.handle, command_digest=command.sha256, cwd=".",
        )
        state.record_shell_approval_fact(requested)
        state.record_shell_approval_fact(replace(requested, kind="approved"))
    finally:
        state.close()

    restarted = AppContext(tmp_path)
    scheduled: list[str] = []
    try:
        monkeypatch.setattr(restarted.shell_approval_workflow, "_schedule", scheduled.append)
        restarted.shell_approval_workflow.start(lambda *_args, **_kwargs: None)
        assert scheduled == ["approval-1"]
    finally:
        restarted.close()


def _session(backend: str, tmp_path: Path) -> DurableSession:
    store = MemoryDurableStore() if backend == "memory" else SQLiteDurableStore(tmp_path / "durable.sqlite3")
    session = DurableSession(store)
    session.admit_input(
        conversation_id="conversation-1",
        request_id="request-1",
        task_id="task-1",
        text="run a shell command",
        provider_snapshot={"provider_id": "local"},
    )
    session.start_task("task-1", state={})
    return session


@pytest.mark.parametrize("backend", ["memory", "sqlite"])
def test_approval_lifecycle_is_durable_and_cas_checked(backend: str, tmp_path: Path) -> None:
    session = _session(backend, tmp_path)
    requested = ApprovalRecord(
        approval_id="approval-1",
        task_id="task-1",
        conversation_id="conversation-1",
        status=ApprovalStatus.REQUESTED,
        command_digest="sha256:command",
        command_ref="artifact:command",
        cwd="E:/project",
    )

    first = session.create_approval(requested)
    assert session.approval("approval-1") == requested
    assert session.commit_sequence() == first.sequence

    approved = replace(requested, status=ApprovalStatus.APPROVED, revision=1)
    session.transition_approval("approval-1", approved, expected_status=ApprovalStatus.REQUESTED)
    assert session.approval("approval-1").status == ApprovalStatus.APPROVED

    with pytest.raises(CommitConflict):
        session.transition_approval(
            "approval-1",
            replace(approved, status=ApprovalStatus.DENIED, revision=2),
            expected_status=ApprovalStatus.REQUESTED,
        )
    session.close()


@pytest.mark.parametrize("backend", ["memory", "sqlite"])
def test_approval_decision_and_task_checkpoint_share_one_commit(backend: str, tmp_path: Path) -> None:
    session = _session(backend, tmp_path)
    requested = ApprovalRecord(
        "approval-1", "task-1", "conversation-1", ApprovalStatus.REQUESTED,
        "sha256:command", "artifact:command", "E:/project",
    )
    session.create_approval(requested)
    current = session.task("task-1")
    next_task = replace(
        current,
        phase="assistant_response_pending",
        revision=current.revision + 1,
        status=TaskStatus.RUNNING,
        state=dict(current.state),
    )
    before = session.commit_sequence()
    session.transition_approval(
        "approval-1",
        replace(requested, status=ApprovalStatus.APPROVED, revision=1),
        expected_status=ApprovalStatus.REQUESTED,
        task=next_task,
    )
    assert session.commit_sequence() == before + 1
    assert session.task("task-1").phase == "assistant_response_pending"
    assert session.approval("approval-1").status == ApprovalStatus.APPROVED
    session.close()


@pytest.mark.parametrize("backend", ["memory", "sqlite"])
def test_approval_claim_and_shell_effect_intent_are_one_durable_commit(
    backend: str, tmp_path: Path,
) -> None:
    session = _session(backend, tmp_path)
    requested = ApprovalRecord(
        "approval-atomic", "task-1", "conversation-1", ApprovalStatus.REQUESTED,
        "sha256:command", "artifact:command", "E:/project",
    )
    session.create_approval(requested)
    approved = replace(requested, status=ApprovalStatus.APPROVED, revision=1)
    session.transition_approval("approval-atomic", approved, expected_status=ApprovalStatus.REQUESTED)
    session.advance_task(task_id="task-1", phase="assistant_response_pending", state={})
    effect = EffectRecord(
        effect_id="shell-effect-1", task_id="task-1", category="shell",
        replay_class="unsafe", args_digest="sha256:args", status="pending",
        intent={"approval_id": "approval-atomic", "command_ref": "artifact:command"},
    )
    before = session.commit_sequence()
    result = session.claim_approval_and_begin_effect("approval-atomic", effect)
    assert result is not None
    assert session.commit_sequence() == before + 1
    assert session.approval("approval-atomic").status == ApprovalStatus.EXECUTION_CLAIMED
    assert session.effect("shell-effect-1") == effect
    assert session.task("task-1").pending_effect_id == "shell-effect-1"
    assert session.claim_approval_and_begin_effect("approval-atomic", effect) is None
    assert session.commit_sequence() == before + 1
    session.close()


@pytest.mark.parametrize("backend", ["memory", "sqlite"])
def test_approval_claim_failure_leaves_authorization_and_effect_untouched(
    backend: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    session = _session(backend, tmp_path)
    requested = ApprovalRecord(
        "approval-rollback", "task-1", "conversation-1", ApprovalStatus.REQUESTED,
        "sha256:command", "artifact:command", "E:/project",
    )
    session.create_approval(requested)
    session.transition_approval(
        "approval-rollback", replace(requested, status=ApprovalStatus.APPROVED, revision=1),
        expected_status=ApprovalStatus.REQUESTED,
    )
    session.advance_task(task_id="task-1", phase="assistant_response_pending", state={})
    effect = EffectRecord(
        "shell-effect-rollback", "task-1", "shell", "unsafe", "sha256:args", "pending",
        {"approval_id": "approval-rollback", "session_id": "conversation-1",
         "run_id": "task-1", "lane": "writer", "operation_id": "operation-task-1"},
    )
    before_task = session.task("task-1")
    before_seq = session.commit_sequence()
    monkeypatch.setattr(session.store, "commit", lambda _mutation: (_ for _ in ()).throw(OSError("disk full")))
    with pytest.raises(OSError, match="disk full"):
        session.claim_approval_and_begin_effect("approval-rollback", effect)
    reopened = DurableSession(session.store)
    assert reopened.approval("approval-rollback").status == ApprovalStatus.APPROVED
    assert reopened.task("task-1") == before_task
    with pytest.raises(KeyError):
        reopened.effect("shell-effect-rollback")
    assert reopened.commit_sequence() == before_seq
    reopened.close()


def test_claimed_shell_effect_is_marked_unknown_after_restart(tmp_path: Path) -> None:
    from types import SimpleNamespace

    from codey.operations.recovery import recover_effects_for_resume

    path = tmp_path / "durable.sqlite3"
    session = DurableSession(SQLiteDurableStore(path))
    session.admit_input(
        conversation_id="conversation-1", request_id="request-1", task_id="task-1",
        text="shell", provider_snapshot={"provider_id": "local"},
    )
    session.start_task("task-1", state={})
    session.advance_task(task_id="task-1", phase="assistant_response_pending", state={})
    requested = ApprovalRecord(
        "approval-restart", "task-1", "conversation-1", ApprovalStatus.REQUESTED,
        "sha256:command", "artifact:command", "E:/project",
    )
    session.create_approval(requested)
    session.transition_approval(
        "approval-restart", replace(requested, status=ApprovalStatus.APPROVED, revision=1),
        expected_status=ApprovalStatus.REQUESTED,
    )
    effect = EffectRecord(
        "shell-effect-restart", "task-1", "shell", "unsafe", "sha256:args", "pending",
        {"approval_id": "approval-restart", "session_id": "conversation-1",
         "run_id": "task-1", "lane": "writer", "operation_id": "operation-task-1"},
    )
    session.claim_approval_and_begin_effect("approval-restart", effect)
    session.close()

    reopened = DurableSession(SQLiteDurableStore(path))
    result = recover_effects_for_resume(
        SimpleNamespace(
            state=SimpleNamespace(durable=reopened), runtime_mutations=None,
            runtime_effects=None, tool_result_delivery=None,
        ),
        session_id="conversation-1", run_id="task-1", project="", task_kind="chat",
    )
    assert result.ok is False
    assert reopened.effect("shell-effect-restart").status == "interrupted"
    assert reopened.task("task-1").phase == "failed"
    reopened.close()
