"""Desktop and headless resolve the active review services through one composition."""
from pathlib import Path

import pytest

from codey.app import headless_runner, task_submit
from codey.app.headless_runner import HeadlessAppContext, HeadlessRequest
from codey.app.task_state import TaskSubmissionState
from codey.operations.task_run import TaskRunDeps
from codey.task.model import TaskSubmission


def test_both_entries_dispatch_each_service_with_same_state_and_review_policy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[str, TaskSubmissionState, dict[str, object]]] = []

    def consensus(state: TaskSubmissionState, **kwargs: object) -> None:
        calls.append(("run_consensus", state, kwargs))

    def review(state: TaskSubmissionState, **kwargs: object) -> None:
        calls.append(("run_review", state, kwargs))

    monkeypatch.setattr("codey.app.consensus_service.run_consensus", consensus)
    monkeypatch.setattr("codey.app.review_service.run_review", review)

    class Sender:
        def send(self, text: str, timeout: float | None = None) -> str:
            raise AssertionError("test advisor should not invoke the provider")

    def submit(deps: TaskRunDeps, request: TaskSubmission) -> None:
        assert deps.project_review
        assert deps.run_consensus is not None and deps.run_review is not None
        assert deps.run_project_audit is None
        deps.run_consensus(task=request.task, selected_provider=Sender(), selected_provider_id=request.provider_id)
        deps.run_review(session_id=request.session_id, project=request.project or "", task=request.task,
                        writer_summary="", changes={}, recent_log="", writer_id=request.provider_id)

    monkeypatch.setattr("codey.operations.task_submission.run_task_submission", submit)
    monkeypatch.setattr(headless_runner, "run_task_submission", submit)
    state = HeadlessAppContext(tmp_path / "desktop-state", port=9222, emit_jsonl=lambda _: None)
    try:
        task_submit.run_task("s", str(tmp_path / "project"), "Inspect", 8, False, "local", "planning_readonly",
                             get_state=lambda: state, review_policy="require_web")
    finally:
        state.close()
    headless_runner.run_headless(HeadlessRequest(project=tmp_path / "project", task="Inspect", intent="planning_readonly",
                                               provider_id="local", state_home=tmp_path / "cli-state", review_policy="require_web"), emit_jsonl=lambda _: None)
    assert [name for name, *_ in calls] == ["run_consensus", "run_review"] * 2
    for first, second in zip(calls[:2], calls[2:], strict=True):
        assert first[1] is not second[1]
        assert first[2]["task"] == second[2]["task"] == "Inspect"
    assert calls[0][2]["selected_provider_id"] == calls[2][2]["selected_provider_id"] == "local"
    assert calls[1][2]["review_policy"] == "require_web"
