"""Regression locks for approval claims and source-run resume recovery."""

from __future__ import annotations

import threading
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
        TaskRuntime(state.runtime_log, lambda _: OperationOutcome.suspended(reason="approval")).run(request)
        _durable_requested_approval(
            state,
            approval_id="approval-cold-start",
            source_run_id="source-run",
        )
    finally:
        state.close()

    restarted = AppContext(tmp_path)
    try:
        pending = restarted.claim_pending_shell_approval("approval-cold-start")
        assert pending is not None
        assert pending["_api_selection"] == selection
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
    setup = SimpleNamespace(
        request=request,
        state=SimpleNamespace(),
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
