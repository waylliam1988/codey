"""providers.preferred stays a soft ranking preference, never an override."""

from __future__ import annotations

import json
import tempfile
from pathlib import Path
from unittest import mock

import pytest

from codey import server
from codey.agent import RunResult
from codey.provider_diagnostics import (
    FAILURE_AUTHENTICATION_REQUIRED,
    ProviderActionError,
    ProviderFailure,
)
from codey.project_config import preferred_provider_for
from codey.task_runner import TaskRequest, TaskRunner


def _failure(kind: str = "response_missing") -> ProviderActionError:
    return ProviderActionError(ProviderFailure(
        "DeepSeek",
        "send",
        "",
        "",
        "missing",
        "now",
        kind,
    ))


class _Provider:
    def __init__(self, name: str) -> None:
        self.name = name
        self.location = f"https://chat.example/{name}"
        self.closed = False

    def new_chat(self, timeout: float | None = None) -> None:
        del timeout

    def send(self, text: str, timeout: float | None = None) -> str:
        del text, timeout
        return '{"tool":"done","args":{"summary":"ok"}}'

    def close(self) -> None:
        self.closed = True


def _write_preferred_config(project: Path, mode: str, provider_id: str) -> None:
    config_dir = project / ".codey"
    config_dir.mkdir(parents=True)
    (config_dir / "config.json").write_text(
        json.dumps({
            "schema_version": 1,
            "providers": {"preferred": {mode: provider_id}},
        }),
        encoding="utf-8",
    )


def _build_runner(state: server.State, *, agent_run) -> TaskRunner:
    return TaskRunner(
        state,
        agent_run=agent_run,
        collect_changes=mock.Mock(
            return_value={"ok": True, "changed_count": 0, "files": [], "diff": ""}
        ),
        run_review=mock.Mock(return_value=None),
        capture_provider_failure=server.capture_provider_failure,
        project_facts=state.project_facts,
        work_checkpoints=state.work_checkpoints,
        run_ledgers=state.run_ledgers,
        run_traces=state.run_traces,
        evidence_ledgers=state.evidence_ledgers,
        managed_outputs=state.managed_outputs,
        knowledge_store=state.knowledge_store,
        is_git_repository=lambda _project: True,
    )


def _run_project_task(project: Path, state: server.State, *, provider_id: str) -> None:
    runner = _build_runner(state, agent_run=_agent_run)
    runner.run(TaskRequest(
        "session-preferred-providers",
        str(project),
        "Inspect the project",
        8,
        False,
        provider_id,
        intent="project",
    ))


def _agent_run(_provider, _project, _task, **_kwargs) -> RunResult:
    raise AssertionError("writer should have been replaced by the fake below")


def test_preferred_provider_for_matches_mode_and_ignores_planning_alias() -> None:
    from codey.project_config import ProjectConfig, ProjectProviderPreference

    config = ProjectConfig(preferred_providers=(
        ProjectProviderPreference(mode="project", provider_id="glm"),
        ProjectProviderPreference(mode="review", provider_id="qwen"),
    ))
    assert preferred_provider_for(config, "project") == "glm"
    assert preferred_provider_for(config, "planning_readonly") == ""
    assert preferred_provider_for(config, "review") == "qwen"
    assert preferred_provider_for(ProjectConfig(), "project") == ""


def test_project_config_reorders_writer_failover_candidates() -> None:
    with tempfile.TemporaryDirectory() as td:
        project = Path(td)
        _write_preferred_config(project, "project", "glm")
        state = server.State(td)
        state.provider_failover_order = lambda: ("deepseek", "stepfun", "glm")
        providers = {
            "deepseek": _Provider("DeepSeek Web"),
            "stepfun": _Provider("StepFun Chat"),
            "glm": _Provider("GLM"),
        }

        calls: list[str] = []

        def agent_run(provider, _project, _task, **_kwargs) -> RunResult:
            calls.append(provider.name)
            if len(calls) == 1:
                raise _failure()
            return RunResult("done", "done", 1)

        runner = TaskRunner(
            state,
            agent_run=agent_run,
            collect_changes=mock.Mock(
                return_value={"ok": True, "changed_count": 0, "files": [], "diff": ""}
            ),
            run_review=mock.Mock(return_value=None),
            capture_provider_failure=server.capture_provider_failure,
            is_git_repository=lambda _project: True,
        )
        with mock.patch.object(
            state,
            "get_provider",
            side_effect=lambda provider_id: providers[provider_id],
        ):
            runner.run(TaskRequest(
                "session-preferred-order",
                str(project),
                "Inspect the project",
                8,
                False,
                "deepseek",
                intent="project",
            ))
            state.wait_for_ghost_sleep(timeout=2)

    # Without the preference the tie-broken order would try stepfun first;
    # the soft preference moves glm to the front of the failover candidates.
    assert [name for name in calls] == ["DeepSeek Web", "GLM"]
    assert state.last_terminal_event["provider"] == "glm"


def test_preference_does_not_override_the_user_selected_provider() -> None:
    with tempfile.TemporaryDirectory() as td:
        project = Path(td)
        _write_preferred_config(project, "project", "glm")
        state = server.State(td)
        providers = {
            "deepseek": _Provider("DeepSeek Web"),
            "stepfun": _Provider("StepFun Chat"),
            "glm": _Provider("GLM"),
        }
        writer_ids: list[str] = []

        def agent_run(provider, _project, _task, **_kwargs) -> RunResult:
            for provider_id, candidate in providers.items():
                if candidate is provider:
                    writer_ids.append(provider_id)
            return RunResult("done", "done", 1)

        runner = TaskRunner(
            state,
            agent_run=agent_run,
            collect_changes=mock.Mock(
                return_value={"ok": True, "changed_count": 0, "files": [], "diff": ""}
            ),
            run_review=mock.Mock(return_value=None),
            capture_provider_failure=server.capture_provider_failure,
            is_git_repository=lambda _project: True,
        )
        with mock.patch.object(
            state,
            "get_provider",
            side_effect=lambda provider_id: providers[provider_id],
        ):
            runner.run(TaskRequest(
                "session-preferred-no-override",
                str(project),
                "Inspect the project",
                8,
                False,
                "stepfun",
                intent="project",
            ))
            state.wait_for_ghost_sleep(timeout=2)

    # The user picked stepfun; the project preference must not replace it,
    # not even when the preferred provider sits earlier in every ranking.
    assert writer_ids == ["stepfun"]
    assert state.last_terminal_event["provider"] == "stepfun"


def test_unavailable_preferred_provider_is_skipped_by_supervisor() -> None:
    with tempfile.TemporaryDirectory() as td:
        project = Path(td)
        _write_preferred_config(project, "project", "glm")
        state = server.State(td)
        state.provider_failover_order = lambda: ("deepseek", "stepfun", "glm")
        # The supervisor's health boundary outranks any project preference.
        state.provider_supervisor.record_failure(
            "glm",
            ProviderFailure(
                "GLM",
                "send",
                "",
                "",
                "login required",
                "now",
                FAILURE_AUTHENTICATION_REQUIRED,
            ),
        )
        providers = {
            "deepseek": _Provider("DeepSeek Web"),
            "stepfun": _Provider("StepFun Chat"),
            "glm": _Provider("GLM"),
        }
        calls: list[str] = []

        def agent_run(provider, _project, _task, **_kwargs) -> RunResult:
            calls.append(provider.name)
            if len(calls) == 1:
                raise _failure()
            return RunResult("done", "done", 1)

        runner = TaskRunner(
            state,
            agent_run=agent_run,
            collect_changes=mock.Mock(
                return_value={"ok": True, "changed_count": 0, "files": [], "diff": ""}
            ),
            run_review=mock.Mock(return_value=None),
            capture_provider_failure=server.capture_provider_failure,
            is_git_repository=lambda _project: True,
        )
        with mock.patch.object(
            state,
            "get_provider",
            side_effect=lambda provider_id: providers[provider_id],
        ):
            runner.run(TaskRequest(
                "session-preferred-unavailable",
                str(project),
                "Inspect the project",
                8,
                False,
                "deepseek",
                intent="project",
            ))
            state.wait_for_ghost_sleep(timeout=2)

    assert calls[1] == "StepFun Chat"
    assert state.last_terminal_event["provider"] == "stepfun"


def test_early_failure_inside_claim_route_window_releases_the_run_slot() -> None:
    with tempfile.TemporaryDirectory() as td:
        state = server.State(td)
        runner = TaskRunner(
            state,
            agent_run=mock.Mock(return_value=RunResult("done", "done", 1)),
            collect_changes=mock.Mock(
                return_value={"ok": True, "changed_count": 0, "files": [], "diff": ""}
            ),
            run_review=mock.Mock(return_value=None),
            capture_provider_failure=server.capture_provider_failure,
            is_git_repository=lambda _project: True,
        )

        with (
            mock.patch.object(
                TaskRunner,
                "_maybe_route_auto",
                side_effect=ValueError("route exploded"),
            ),
            pytest.raises(ValueError),
        ):
            runner.run(TaskRequest(
                "session-early-error",
                td,
                "Inspect the project",
                8,
                False,
                "deepseek",
                intent="auto",
            ))
            state.wait_for_ghost_sleep(timeout=2)

    # The started run must end with a bounded error terminal event, not a
    # permanently busy slot.
    terminal = state.last_terminal_event
    assert terminal is not None and terminal["stop_reason"] == "error"
    assert "route exploded" in str(terminal["summary"])
    with state.lock:
        assert state.active_run is None and not state.busy


if __name__ == "__main__":
    raise SystemExit(__import__("unittest").main())
