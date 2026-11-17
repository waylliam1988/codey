from __future__ import annotations

import tempfile
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import Protocol
from unittest import mock

from codey.app.context import AppContext
from codey.app.event_bus import EventSubscriber
from codey.ghost.payload_types import mapping_fields
from codey.operations.task_entry import run_task_submission
from codey.operations.task_run import TaskRunDeps
from codey.providers import controls as provider_controls
from codey.providers.diagnostics import ProviderFailure, capture_provider_failure
from codey.research.pipeline import ResearchIterationRun
from codey.research.run_result import ResearchRunResult
from codey.reviews.core import ReviewFinding, ReviewResult
from codey.runtime.core import cancellation
from codey.runtime.core.run_result import RunResult
from codey.task.model import TaskSubmission
from codey.types.json_value import JsonObject
from codey.workspace.changes import ChangeTracker
from codey.workspace.changes import collect_changes as collect_project_changes

RESEARCH_ITERATION = "codey.operations.research_iteration.run_research_iteration"


class _Provider:
    name = "DeepSeek Web"
    location = "https://chat.deepseek.com/"

    def __init__(self, reply: str = "ok") -> None:
        self.reply = reply
        self.prompts: list[str] = []
        self.new_chat_called = False
        self.new_chat_timeout: float | None = None
        self.send_timeout: float | None = None
        self.closed = False

    def new_chat(self, timeout: float | None = None) -> None:
        self.new_chat_timeout = timeout
        self.new_chat_called = True

    def send(self, text: str, timeout: float | None = None) -> str:
        self.send_timeout = timeout
        self.prompts.append(text)
        return self.reply

    def close(self) -> None:
        self.closed = True


class _CancelProvider(_Provider):
    def send(self, text: str, timeout: float | None = None) -> str:
        del text, timeout
        raise cancellation.TaskCancelled("task stopped")


class _TeachCancelProvider(_Provider):
    def send(self, text: str, timeout: float | None = None) -> str:
        del text, timeout
        raise provider_controls.ControlTeachCancelled("teach cancelled")


class _ChangesCollector(Protocol):
    def __call__(
        self, project: str | Path | None, tracker: ChangeTracker | None = None,
    ) -> dict[str, object]: ...


def _empty_changes(project: str | Path | None, tracker: ChangeTracker | None = None) -> dict[str, object]:
    return {"ok": True, "changed_count": 0, "files": [], "diff": ""}


def _reviewable_changes(project: str | Path | None, tracker: ChangeTracker | None = None) -> dict[str, object]:
    return {
        "ok": True,
        "changed_count": 1,
        "files": [{"path": "app.py", "status": "M"}],
        "diff": "diff --git a/app.py b/app.py\n-old\n+new\n",
    }


def _project_changes(project: str | Path | None, tracker: ChangeTracker | None = None) -> dict[str, object]:
    value: object = collect_project_changes(project, tracker)
    assert mapping_fields(value)
    return dict(value)


def _capture_failure(
    *, model: str, action: str, page: object, error: BaseException, now: datetime | None = None,
) -> ProviderFailure:
    return capture_provider_failure(model=model, action=action, page=page, error=error, now=now)


def _git_project(project: str | Path) -> bool:
    return True


def _runner(
    state: AppContext,
    *,
    agent_run: mock.Mock | None = None,
    collect_changes: _ChangesCollector = _empty_changes,
    run_review: mock.Mock | None = None,
    is_git_repository: Callable[[str | Path], bool] | None = None,
) -> TaskRunDeps:
    return TaskRunDeps(state=state,
        agent_run=agent_run or mock.Mock(return_value=RunResult("done", "done", 1)),
        collect_changes=collect_changes,
        run_review=run_review or mock.Mock(return_value=None),
        capture_provider_failure=_capture_failure,
        project_facts=state.project_facts,
        work_checkpoints=state.work_checkpoints,
        workspace_revisions=state.workspace_revisions,
        run_ledgers=state.run_ledgers,
        managed_outputs=state.managed_outputs,
        knowledge_store=state.knowledge_store,
        is_git_repository=is_git_repository or _git_project,
        runtime_mutations=state.runtime_mutations,
        runtime_effects=state.runtime_effects,
    )


def _terminal(state: AppContext) -> JsonObject:
    event = state.run_registry.last_terminal_event()
    assert event is not None
    return event


def _done_events(state: AppContext) -> list[JsonObject]:
    return [_terminal(state)]


def _drain(events: EventSubscriber) -> list[dict[str, object]]:
    emitted: list[dict[str, object]] = []
    while not events.empty():
        value: object = events.get_nowait()
        assert mapping_fields(value)
        emitted.append(dict(value))
    return emitted


def _observations(state: AppContext) -> tuple[dict[str, object], ...]:
    store = state.ghost_observations
    assert store is not None
    return store.read_committed(session_id="session-1")


def _run_and_wait_for_local_maintenance(
    runner: TaskRunDeps,
    state: AppContext,
    request: TaskSubmission,
) -> None:
    run_task_submission(runner, request)
    assert state.wait_for_ghost_sleep(timeout=30)


class _FailingProvider(_Provider):
    def send(self, text: str, timeout: float | None = None) -> str:
        del text, timeout
        raise RuntimeError("offline")


def test_chat_research_tools_continue_shared_kernel_without_mode_redispatch(public_source_dns: None) -> None:
    import json

    from codey.research.ledger import ResearchLedger
    from codey.research.tools import ResearchTools

    with tempfile.TemporaryDirectory() as td:
        state = AppContext(td)
        events = state.subscribe()
        payloads: list[JsonObject] = [
            {"tool": "web_search", "args": {"query": "version"}},
            {"tool": "open_url", "args": {"url": "https://example.com/version"}},
            {"tool": "done", "args": {"summary": "Observed version information."}},
        ]
        replies = iter(json.dumps(payload) for payload in payloads)

        class ResearchProvider(_Provider):
            def send(self, text: str, timeout: float | None = None) -> str:
                self.prompts.append(text)
                return next(replies)

        main_provider = ResearchProvider()

        class Search:
            def search(self, query: str, limit: int = 8) -> list[JsonObject]:
                return [{"url": "https://example.com/version", "title": "Version"}]

            def fetch(self, url: str) -> dict[str, object]:
                return {"status": "ok", "url": url, "title": "Version",
                        "text": "VERSION_KERNEL_MARKER", "truncated": False}

        tools = ResearchTools(search=Search(), store=None, changes=None, ledger=ResearchLedger())
        ledger = tools.ledger
        unused = mock.Mock(side_effect=AssertionError("mode redispatch must not run"))
        with (
            mock.patch.object(state, "get_provider", return_value=main_provider),
            mock.patch(RESEARCH_ITERATION, unused),
            mock.patch("codey.operations.task_entry._entry_executors", return_value=(None, None, tools)),
        ):
            _run_and_wait_for_local_maintenance(_runner(state), state,
                TaskSubmission("session-1", None, "查一下今天的版本变化", 8, False, "deepseek",
                               requested_capabilities=("web.read",)))
            emitted = _drain(events)
            done = _done_events(state)[0]
            observations = _observations(state)
    assert done["stop_reason"] == "done"
    assert done["turns"] == 3
    assert len(main_provider.prompts) == 3
    assert "VERSION_KERNEL_MARKER" in main_provider.prompts[-1]
    assert ledger.final_url_set() == {"https://example.com/version"}
    unused.assert_not_called()
    assert any(event["type"] == "task_start" for event in emitted)
    assert len(observations) == 1
    assert observations[0]["run_id"] == done["run_id"]
    assert observations[0]["user_text"] == "查一下今天的版本变化"
    assert "Auto plan" not in str(observations[0]["user_text"])


def test_chat_preserves_user_no_edit_constraint() -> None:
    """A direct answer preserves the user constraint without starting a writer."""
    with tempfile.TemporaryDirectory() as td:
        project = Path(td, "project")
        project.mkdir()
        state = AppContext(Path(td, "state"))
        main_provider = _Provider("方案如下：只读不改。")
        agent_run = mock.Mock(return_value=RunResult("plan", "done", 1))
        runner = _runner(
            state,
            agent_run=agent_run,
        )

        with mock.patch.object(state, "get_provider", return_value=main_provider):
            _run_and_wait_for_local_maintenance(
                runner,
                state,
                TaskSubmission(
                    "session-1",
                    str(project),
                    "先别改代码，只给我一个修改方案",
                    8,
                    False,
                    "deepseek",
                ),
            )

    agent_run.assert_not_called()
    assert len(main_provider.prompts) == 1
    assert _terminal(state)["mode"] == "chat"
    assert _terminal(state)["summary"] == "方案如下：只读不改。"
    assert state.workspace_leases.leases == {}


def test_chat_preserves_user_no_file_access_constraint() -> None:
    with tempfile.TemporaryDirectory() as td:
        project = Path(td, "project")
        project.mkdir()
        state = AppContext(Path(td, "state"))
        main_provider = _Provider("plain chat")
        agent_run = mock.Mock(return_value=RunResult("should not run", "done", 1))
        runner = _runner(
            state,
            agent_run=agent_run,
        )

        with mock.patch.object(state, "get_provider", return_value=main_provider):
            _run_and_wait_for_local_maintenance(
                runner,
                state,
                TaskSubmission(
                    "session-1",
                    str(project),
                    "不要读写项目文件，只普通聊一下这个想法。",
                    8,
                    False,
                    "deepseek",
                ),
            )

            observations = _observations(state)

    agent_run.assert_not_called()
    assert main_provider.prompts
    assert len(main_provider.prompts) == 1
    assert _terminal(state)["mode"] == "chat"
    assert _terminal(state)["summary"] == "plain chat"
    assert len(observations) == 1


def test_attached_workspace_keeps_chat_start_and_terminal_mode() -> None:
    """An attached workspace keeps Chat presentation throughout the run."""
    with tempfile.TemporaryDirectory() as td:
        project = Path(td, "project")
        project.mkdir()
        state = AppContext(Path(td, "state"))
        main_provider = _Provider("chat reply")
        agent_run = mock.Mock(return_value=RunResult("should not run", "done", 1))
        runner = _runner(
            state,
            agent_run=agent_run,
        )
        events = state.subscribe()

        with mock.patch.object(state, "get_provider", return_value=main_provider):
            _run_and_wait_for_local_maintenance(
                runner,
                state,
                TaskSubmission(
                    "session-1",
                    str(project),
                    "解释一下这个项目的整体设计，不要改文件",
                    8,
                    False,
                    "deepseek",
                ),
            )

        emitted = _drain(events)

    start = next(event for event in emitted if event["type"] == "task_start")
    agent_run.assert_not_called()
    assert main_provider.prompts
    assert len(main_provider.prompts) == 1
    assert _terminal(state)["summary"] == "chat reply"
    assert start["mode"] == "chat"
    assert _terminal(state)["mode"] == "chat"


def test_explicit_research_uses_research_workflow() -> None:
    with tempfile.TemporaryDirectory() as td:
        state = AppContext(td)
        runner = _runner(state)
        research_iteration = mock.Mock(
            return_value=ResearchIterationRun(
                result=ResearchRunResult("q", "manual research", "done", 1)
            )
        )

        with (
            mock.patch.object(state, "get_provider", return_value=_Provider()),
            mock.patch(RESEARCH_ITERATION, research_iteration),
        ):
            _run_and_wait_for_local_maintenance(
                runner,
                state,
                TaskSubmission(
                    "session-1",
                    None,
                    "Research storage",
                    8,
                    False,
                    "deepseek",
                    intent="research",
                ),
            )

    assert _terminal(state)["mode"] == "research"
    assert research_iteration.call_count == 1


def test_workspace_provider_failure_does_not_resend() -> None:
    """A failed workspace request settles once with structured failure evidence."""
    with tempfile.TemporaryDirectory() as td:
        project = Path(td, "project")
        project.mkdir()
        state = AppContext(Path(td, "state"))
        agent_run = mock.Mock(return_value=RunResult("fixed", "done", 1))
        runner = _runner(
            state,
            agent_run=agent_run,
        )

        with mock.patch.object(state, "get_provider", return_value=_FailingProvider()):
            _run_and_wait_for_local_maintenance(
                runner,
                state,
                TaskSubmission(
                    "session-1",
                    str(project),
                    "修复测试",
                    8,
                    False,
                    "deepseek",
                ),
            )

    agent_run.assert_not_called()
    assert _terminal(state)["stop_reason"] == "provider_failure"


def test_workspace_cancellation_stops_task_without_second_runner() -> None:
    with tempfile.TemporaryDirectory() as td:
        project = Path(td, "project")
        project.mkdir()
        state = AppContext(Path(td, "state"))
        agent_run = mock.Mock(return_value=RunResult("fixed", "done", 1))
        main_provider = _CancelProvider("{}")
        runner = _runner(
            state,
            agent_run=agent_run,
        )

        with mock.patch.object(state, "get_provider", return_value=main_provider):
            _run_and_wait_for_local_maintenance(
                runner,
                state,
                TaskSubmission(
                    "session-1",
                    str(project),
                    "修复测试",
                    8,
                    False,
                    "deepseek",
                ),
            )

    agent_run.assert_not_called()
    assert _terminal(state)["stop_reason"] == "stopped"
    assert _terminal(state)["mode"] == "chat"
    assert main_provider.closed


def test_workspace_teach_cancellation_stops_task_without_second_runner() -> None:
    with tempfile.TemporaryDirectory() as td:
        project = Path(td, "project")
        project.mkdir()
        state = AppContext(Path(td, "state"))
        agent_run = mock.Mock(return_value=RunResult("fixed", "done", 1))
        main_provider = _TeachCancelProvider("{}")
        runner = _runner(
            state,
            agent_run=agent_run,
        )

        with mock.patch.object(state, "get_provider", return_value=main_provider):
            _run_and_wait_for_local_maintenance(
                runner,
                state,
                TaskSubmission(
                    "session-1",
                    str(project),
                    "修复测试",
                    8,
                    False,
                    "deepseek",
                ),
            )

    agent_run.assert_not_called()
    assert _terminal(state)["stop_reason"] == "stopped"
    assert _terminal(state)["mode"] == "chat"
    assert main_provider.closed


def test_ghost_disabled_preserves_chat_execution_without_learning() -> None:
    """Disabling learning does not disable the shared workspace execution."""
    with tempfile.TemporaryDirectory() as td:
        project = Path(td, "project")
        project.mkdir()
        state = AppContext(Path(td, "state"))
        assert state.ghost_inbox is not None
        state.ghost_inbox.set_learning_enabled(False)
        main_provider = _Provider("fixed by chat")
        agent_run = mock.Mock(return_value=RunResult("fixed", "done", 1))
        runner = _runner(
            state,
            agent_run=agent_run,
        )

        with mock.patch.object(state, "get_provider", return_value=main_provider):
            _run_and_wait_for_local_maintenance(
                runner,
                state,
                TaskSubmission(
                    "session-1",
                    str(project),
                    "修复测试",
                    8,
                    False,
                    "deepseek",
                ),
            )

        observations = _observations(state)

    agent_run.assert_not_called()
    assert _terminal(state)["mode"] == "chat"
    assert _terminal(state)["summary"] == "fixed by chat"
    assert observations == ()


def test_explicit_review_does_not_start_writer_or_repair() -> None:
    """An explicit Review request does not start editing or automatic repair."""
    with tempfile.TemporaryDirectory() as td:
        project = Path(td, "project")
        project.mkdir()
        state = AppContext(Path(td, "state"))
        main_provider = _Provider()
        agent_run = mock.Mock(return_value=RunResult("should not run", "done", 1))
        review = ReviewResult(
            "changes_requested",
            "One issue",
            [ReviewFinding("app.py", "Bug remains", "Fix the condition")],
        )
        run_review = mock.Mock(return_value=("qwen", review))
        runner = _runner(
            state,
            agent_run=agent_run,
            collect_changes=_reviewable_changes,
            run_review=run_review,
        )

        with mock.patch.object(state, "get_provider", return_value=main_provider):
            _run_and_wait_for_local_maintenance(
                runner,
                state,
                TaskSubmission(
                    "session-1",
                    str(project),
                    "review 一下这次 diff，有问题只列 findings，不要修改",
                    8,
                    False,
                    "deepseek",
                    intent="review",
                ),
            )

    agent_run.assert_not_called()
    run_review.assert_called_once()
    assert _terminal(state)["mode"] == "review"
    assert _terminal(state)["changed"] is False
    summary = _terminal(state)["summary"]
    assert isinstance(summary, str) and "Bug remains" in summary
    assert state.workspace_leases.leases == {}


def test_review_only_provider_failure_is_unavailable_and_not_committed_as_success() -> None:
    with tempfile.TemporaryDirectory() as td:
        project = Path(td, "project")
        project.mkdir()
        state = AppContext(Path(td, "state"))
        main_provider = _Provider()
        run_review = mock.Mock(side_effect=RuntimeError("review provider down"))
        agent_run = mock.Mock(return_value=RunResult("done", "done", 1))
        runner = _runner(
            state,
            agent_run=agent_run,
            collect_changes=_reviewable_changes,
            run_review=run_review,
        )

        with mock.patch.object(state, "get_provider", return_value=main_provider):
            _run_and_wait_for_local_maintenance(
                runner,
                state,
                TaskSubmission(
                    "session-1",
                    str(project),
                    "review 一下这次 diff，不要修改",
                    8,
                    False,
                    "deepseek",
                    intent="review",
                ),
            )

        observations = _observations(state)

    run_review.assert_called_once()
    agent_run.assert_not_called()
    assert observations == ()
    assert _terminal(state)["stop_reason"] == "review_unavailable"
    terminal_review = _terminal(state)["review"]
    assert isinstance(terminal_review, dict)
    assert terminal_review["status"] == "unavailable"
    assert _terminal(state)["mode"] == "review"
    assert _terminal(state)["changed"] is False
    assert _terminal(state)["summary"] == "Review unavailable. No files were changed."


def test_review_only_uses_snapshot_diff_for_non_git_project() -> None:
    with tempfile.TemporaryDirectory() as td:
        project = Path(td, "project")
        project.mkdir()
        target = project / "app.py"
        target.write_text("old\n", encoding="utf-8")
        state = AppContext(Path(td, "state"))
        tracker = state.change_tracker_for(project, persistent=True)
        tracker.capture_before("app.py")
        target.write_text("new\n", encoding="utf-8")
        tracker.capture_after("app.py")
        main_provider = _Provider()
        review = ReviewResult("approved", "Looks good", [])
        run_review = mock.Mock(return_value=("glm", review))
        runner = _runner(
            state,
            collect_changes=_project_changes,
            run_review=run_review,
            is_git_repository=lambda _project: False,
        )

        with mock.patch.object(state, "get_provider", return_value=main_provider):
            _run_and_wait_for_local_maintenance(
                runner,
                state,
                TaskSubmission(
                    "session-1",
                    str(project),
                    "review 一下这次 diff，不要修改",
                    8,
                    False,
                    "deepseek",
                    intent="review",
                ),
            )

    assert _terminal(state)["mode"] == "review"
    assert _terminal(state)["changed"] is False
    call = run_review.call_args
    assert call is not None
    raw_kwargs: object = call.kwargs
    assert mapping_fields(raw_kwargs)
    changes = raw_kwargs["changes"]
    assert mapping_fields(changes)
    assert changes["mode"] == "snapshot"
