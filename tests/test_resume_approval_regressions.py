"""Regression locks for approval claims and source-run resume recovery."""

from __future__ import annotations

import threading
from dataclasses import replace
from types import SimpleNamespace

from codey.app.context import AppContext
from codey.operations import task_run
from codey.runtime.core.api_selection import ApiRunSelection
from codey.runtime.core.outcome import OperationOutcome
from codey.runtime.effects.shell_approval_records import ShellApprovalFact
from codey.runtime.write.task_runtime import TaskRuntime
from codey.task.model import ResumeRun, TaskSubmission


def _durable_requested_approval(
    state: AppContext,
    *,
    approval_id: str,
    source_run_id: str,
    command: str = "python -V",
) -> None:
    try:
        state.durable.task(source_run_id)
    except KeyError:
        state.durable.admit_input(
            conversation_id="session",
            request_id=source_run_id,
            task_id=source_run_id,
            text="shell approval source",
            provider_snapshot={"provider_id": "local"},
        )
        state.durable.start_task(source_run_id, state={})
        state.durable.advance_task(
            task_id=source_run_id, phase="assistant_response_pending", state={}
        )
    else:
        current = state.durable.task(source_run_id)
        if current.phase in {"accepted", "dispatching"}:
            state.durable.start_task(source_run_id, state={})
            state.durable.advance_task(
                task_id=source_run_id, phase="assistant_response_pending", state={}
            )
    assert state.managed_outputs is not None
    artifact = state.managed_outputs.write_json_artifact(
        session_id="session",
        run_id=source_run_id,
        payload={
            "command": command,
            "cwd": ".",
            "project": str(state.state_home),
            "attached_project": str(state.state_home),
            "intent": "chat",
            "provider": "local",
            "continue_after": True,
            "max_turns": 4,
            "call_id": "",
            "window_ref": None,
        },
    )
    state.record_shell_approval_fact(
        ShellApprovalFact(
            approval_id=approval_id,
            session_id="session",
            source_run_id=source_run_id,
            kind="requested",
            command_ref=artifact.handle,
            command_digest=artifact.sha256,
            cwd=".",
        )
    )


def test_durable_approval_claim_is_single_winner_under_concurrency(tmp_path) -> None:
    state = AppContext(tmp_path)
    try:
        _durable_requested_approval(
            state,
            approval_id="approval-race",
            source_run_id="source-run",
        )
        state.record_shell_approval_fact(ShellApprovalFact(
            approval_id="approval-race",
            session_id="session",
            source_run_id="source-run",
            kind="approved",
            command_ref=state.durable.approval("approval-race").command_ref,
            command_digest=state.durable.approval("approval-race").command_digest,
            cwd=".",
        ))
        barrier = threading.Barrier(2)
        results: list[dict | None] = []

        def claim() -> None:
            barrier.wait()
            results.append(state.claim_pending_shell_approval("approval-race"))

        threads = [threading.Thread(target=claim) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=5)

        assert not any(thread.is_alive() for thread in threads)
        assert sum(result is not None for result in results) == 1
        facts = state.shell_approval_records.read("approval-race")
        assert [fact.kind for fact in facts].count("execution_claimed") == 1
        assert facts[-1].kind == "execution_claimed"
    finally:
        state.close()


def test_cold_start_rebuild_restores_source_run_api_selection(tmp_path) -> None:
    selection = ApiRunSelection(
        "local",
        "source-revision",
        "source-model",
        "openai-responses",
        True,
    )
    state = AppContext(tmp_path)
    try:
        source = state.reserve_run(
            session_id="session",
            project=str(tmp_path),
            task="source task",
            provider_id="local",
            run_id="source-run",
        )
        assert source is not None
        request = TaskSubmission(
            "session",
            str(tmp_path),
            "source task",
            4,
            False,
            "local",
            run_id="source-run",
            model_selection=selection.to_payload(),
        )
        from codey.providers.snapshot import provider_snapshot

        state.durable.admit_input(
            conversation_id="session",
            request_id="source-run",
            task_id="source-run",
            text="source task",
            provider_snapshot=provider_snapshot("local", selection),
            task_state={"task": "source task", "provider_id": "local"},
            initial_phase="dispatching",
        )
        TaskRuntime(state.runtime_log, lambda _: OperationOutcome.suspended(reason="approval")).run(request)
        _durable_requested_approval(
            state,
            approval_id="approval-cold-start",
            source_run_id="source-run",
        )
        requested = state.shell_approval_records.latest("approval-cold-start")
        assert requested is not None
        # A durable task must record the explicit user decision before the
        # execution claim; requested -> execution_claimed is not legal.
        state.record_shell_approval_fact(replace(requested, kind="approved"))
    finally:
        state.close()

    restarted = AppContext(tmp_path)
    try:
        pending = restarted.claim_pending_shell_approval("approval-cold-start")
        assert pending is not None
        assert pending["_api_selection"] == selection
    finally:
        restarted.close()


def test_restart_rehydrates_pending_approval_into_ui_state(tmp_path) -> None:
    """A restart must restore the approval card, not only accept its old id."""
    state = AppContext(tmp_path)
    try:
        _durable_requested_approval(
            state,
            approval_id="approval-ui-restart",
            source_run_id="source-run",
            command="python --version",
        )
    finally:
        state.close()

    restarted = AppContext(tmp_path)
    try:
        payload = restarted.run_state_payload()
        pending = payload["pending_event"]
        assert pending is not None
        assert pending["type"] == "shell_request"
        assert pending["id"] == "approval-ui-restart"
        assert pending["command"] == "python --version"
        assert restarted.pending_shell_approvals()["approval-ui-restart"]["ui_event"]["id"] == (
            "approval-ui-restart"
        )
    finally:
        restarted.close()


def test_rehydrated_approval_can_be_denied_and_resume_source_run(tmp_path) -> None:
    from codey.app import api as app_api

    state = AppContext(tmp_path)
    try:
        _durable_requested_approval(
            state,
            approval_id="approval-deny-restart",
            source_run_id="source-run",
            command="python --version",
        )
    finally:
        state.close()

    restarted = AppContext(tmp_path)
    submit = lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("continuation task must not be submitted"))
    try:
        status, payload = app_api.shell_approval_response(
            restarted,
            {"id": "approval-deny-restart", "approved": False},
            submit_task_after_slot_release=submit,
        )
        assert status == 202
        assert restarted.shell_approval_workflow.wait(5)
        assert restarted.shell_approval_workflow.status("approval-deny-restart")["decision"] == "deny"
        approval = restarted.durable.approval("approval-deny-restart")
        assert str(approval.status) == "denied"
        assert approval.continuation_run_id == "source-run"
        assert len(restarted.durable.tasks()) == 1
    finally:
        restarted.close()


def test_structured_resume_recovery_reads_the_declared_source_run(monkeypatch) -> None:
    request = TaskSubmission.from_input(
        session_id="session",
        project=None,
        max_turns=4,
        provider_id="local",
        input=ResumeRun("source-run", "continue"),
        run_id="new-run",
    )
    work = SimpleNamespace(operation=None)
    seen: dict[str, object] = {}

    monkeypatch.setattr(
        task_run,
        "build_run_work",
        lambda *_args, **_kwargs: (work, None),
    )

    def recover(_deps, **kwargs):
        seen.update(kwargs)
        return SimpleNamespace(
            ok=True,
            recovered_tool_outcomes=(),
            settled_tool_outcomes=(),
            recovered_tool_result_batch_id="",
        )

    monkeypatch.setattr(task_run, "recover_effects_for_resume", recover)
    durable = SimpleNamespace(task=lambda _run_id: SimpleNamespace(state={"task_kind": "chat"}))
    setup = SimpleNamespace(
        request=request,
        state=SimpleNamespace(durable=durable),
        session_id="session",
        run_id="new-run",
        project=None,
        provider_id="local",
        max_turns=4,
        task_kind="chat",
        task=request.task,
        continue_task=True,
        recovered_resume=False,
        trace=None,
        project_config_result=SimpleNamespace(config=SimpleNamespace(ignored_paths=())),
    )

    workload, failure = task_run._build_workload(SimpleNamespace(), setup)

    assert failure is None
    assert workload is not None
    assert seen["run_id"] == "source-run"
    assert request.previous_run_id == "source-run"


def test_source_delivery_batch_is_not_reused_by_new_resume_run(monkeypatch) -> None:
    request = TaskSubmission.from_input(
        session_id="session", project=None, max_turns=4, provider_id="local",
        input=ResumeRun("source-run", "continue"), run_id="new-run",
    )
    work = SimpleNamespace(operation=None)
    monkeypatch.setattr(task_run, "build_run_work", lambda *_args, **_kwargs: (work, None))
    monkeypatch.setattr(
        task_run, "recover_effects_for_resume",
        lambda *_args, **_kwargs: SimpleNamespace(
            ok=True, recovered_tool_outcomes=(), settled_tool_outcomes=(),
            recovered_tool_result_batch_id="deliv-source-1",
        ),
    )
    durable = SimpleNamespace(task=lambda _run_id: SimpleNamespace(state={"task_kind": "chat"}))
    setup = SimpleNamespace(
        request=request, state=SimpleNamespace(durable=durable), session_id="session", run_id="new-run",
        project=None, provider_id="local", max_turns=4, task_kind="chat", task=request.task,
        continue_task=True, recovered_resume=False, trace=None,
        project_config_result=SimpleNamespace(config=SimpleNamespace(ignored_paths=())),
    )
    workload, failure = task_run._build_workload(SimpleNamespace(), setup)
    assert failure is None and workload is not None
    assert workload.recovered_tool_result_batch_id == ""


def test_stop_persists_abandoned_so_old_approval_cannot_resume(tmp_path) -> None:
    state = AppContext(tmp_path)
    try:
        _durable_requested_approval(state, approval_id="stale", source_run_id="source-run")
        state.add_pending_shell_approval("stale", {
            "session_id": "session", "run_id": "source-run", "command": "python -V",
            "cwd": ".", "project": str(tmp_path), "attached_project": str(tmp_path),
            "intent": "chat", "provider": "local", "continue_after": False,
            "max_turns": 1, "call_id": "", "window_ref": None,
            "command_preview": "python -V", "command_sha256": "".join("0" for _ in range(64)),
            "command_chars": 10, "command_truncated": False,
        })
        state.request_stop()
    finally:
        state.close()
    restarted = AppContext(tmp_path)
    try:
        assert restarted.claim_pending_shell_approval("stale") is None
        assert restarted.shell_approval_records.latest("stale").kind == "abandoned"
    finally:
        restarted.close()


def test_denial_claim_is_a_durable_verdict_against_later_allow(tmp_path) -> None:
    state = AppContext(tmp_path)
    try:
        _durable_requested_approval(state, approval_id="decision-race", source_run_id="source-run")
        denied = state.claim_pending_shell_approval("decision-race", claim_execution=False)
        assert denied is not None
        assert state.claim_pending_shell_approval("decision-race") is None
        assert state.shell_approval_records.latest("decision-race").kind == "denied"
    finally:
        state.close()
