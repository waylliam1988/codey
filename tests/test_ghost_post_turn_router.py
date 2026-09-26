from __future__ import annotations

import tempfile
from pathlib import Path
from unittest import mock

from codey.agents.runner import RunResult
from codey.app import server
from codey.app import task_submit as task_submit
from codey.operations.task_entry import TaskRunDeps, run_task_submission
from codey.providers import controls as provider_controls
from codey.research.pipeline import ResearchIterationRun
from codey.research.runner import ResearchRunResult
from codey.reviews.core import ReviewFinding, ReviewResult
from codey.runtime.core import cancellation
from codey.task.model import TaskSubmission
from codey.workspace.changes import collect_changes as collect_project_changes

RESEARCH_ITERATION = "codey.operations.research_flow.run_research_iteration"


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


def _empty_changes(*_args, **_kwargs) -> dict:
    return {"ok": True, "changed_count": 0, "files": [], "diff": ""}


def _reviewable_changes(*_args, **_kwargs) -> dict:
    return {
        "ok": True,
        "changed_count": 1,
        "files": [{"path": "app.py", "status": "M"}],
        "diff": "diff --git a/app.py b/app.py\n-old\n+new\n",
    }


def _runner(
    state: server.AppContext,
    *,
    agent_run=None,
    collect_changes=_empty_changes,
    run_review=None,
    router_provider_factory=None,
    is_git_repository=None,
) -> TaskRunDeps:
    return TaskRunDeps(state=state,
        agent_run=agent_run or mock.Mock(return_value=RunResult("done", "done", 1)),
        collect_changes=collect_changes,
        run_review=run_review or mock.Mock(return_value=None),
        capture_provider_failure=task_submit.capture_provider_failure,
        project_facts=state.project_facts,
        work_checkpoints=state.work_checkpoints,
        workspace_revisions=state.workspace_revisions,
        run_ledgers=state.run_ledgers,
        managed_outputs=state.managed_outputs,
        knowledge_store=state.knowledge_store,
        is_git_repository=is_git_repository or (lambda _project: True),
        ghost_router_provider_factory=router_provider_factory,
        runtime_mutations=state.runtime_mutations,
        runtime_effects=state.runtime_effects,
    )


def _done_events(state: server.AppContext) -> list[dict]:
    return list(state.run_registry.last_terminal_event() and [state.run_registry.last_terminal_event()] or [])


def _run_and_wait_for_local_maintenance(
    runner: TaskRunDeps,
    state: server.AppContext,
    request: TaskSubmission,
) -> None:
    run_task_submission(runner, request)
    assert state.wait_for_ghost_sleep(timeout=30)


class _FailingProvider(_Provider):
    def send(self, text: str, timeout: float | None = None) -> str:
        del text, timeout
        raise RuntimeError("offline")


def test_auto_router_result_is_consumed_before_task_start_and_main_connect() -> None:
    """Unified auto: the first normal call requests research; no retired
    router call happens and the PLAN is forwarded, not discarded."""
    with tempfile.TemporaryDirectory() as td:
        state = server.AppContext(td)
        events = state.subscribe()
        main_provider = _Provider("ACTION: research\nPLAN: 查今天的版本变化")
        router_factory = mock.Mock(side_effect=AssertionError("retired router must not run"))
        order: list[str] = []

        def get_provider(_provider_id: str):
            order.append("main")
            return main_provider

        runner = _runner(state, router_provider_factory=router_factory)
        research_iteration = mock.Mock(
            return_value=ResearchIterationRun(
                result=ResearchRunResult("q", "researched", "done", 1)
            )
        )

        with (
            mock.patch.object(state, "get_provider", side_effect=get_provider),
            mock.patch(RESEARCH_ITERATION, research_iteration),
        ):
            _run_and_wait_for_local_maintenance(
                runner,
                state,
                TaskSubmission("session-1", None, "查一下今天的版本变化", 8, False, "deepseek"),
            )

            emitted = []
            while not events.empty():
                emitted.append(events.get_nowait())

            start = next(event for event in emitted if event["type"] == "task_start")
            done = _done_events(state)[0]
            observations = state.ghost_observations.read_committed(session_id="session-1")

    router_factory.assert_not_called()
    assert order == ["main"]
    assert len(main_provider.prompts) == 1
    assert start["mode"] == "chat"
    assert done["mode"] == "research"
    assert research_iteration.call_count == 1
    assert "Ghost" not in main_provider.prompts[0]
    assert "Codey" not in main_provider.prompts[0]
    assert len(observations) == 1
    assert observations[0]["run_id"] == done["run_id"]


def test_auto_router_hard_rule_blocks_writer_when_user_says_not_to_edit() -> None:
    """User forbids edits and the model answers directly: unified auto stays
    in chat and never touches the project writer."""
    with tempfile.TemporaryDirectory() as td:
        project = Path(td, "project")
        project.mkdir()
        state = server.AppContext(Path(td, "state"))
        main_provider = _Provider("方案如下：只读不改。")
        agent_run = mock.Mock(return_value=RunResult("plan", "done", 1))
        runner = _runner(
            state,
            agent_run=agent_run,
            router_provider_factory=mock.Mock(
                side_effect=AssertionError("retired router must not run")),
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
    assert state.run_registry.last_terminal_event()["mode"] == "chat"
    assert state.run_registry.last_terminal_event()["summary"] == "方案如下：只读不改。"
    assert getattr(state, "_project_writer_leases", {}) == {}


def test_auto_router_hard_rule_blocks_project_access_when_user_forbids_files() -> None:
    with tempfile.TemporaryDirectory() as td:
        project = Path(td, "project")
        project.mkdir()
        state = server.AppContext(Path(td, "state"))
        main_provider = _Provider("plain chat")
        agent_run = mock.Mock(return_value=RunResult("should not run", "done", 1))
        router_factory = mock.Mock(
            side_effect=AssertionError("retired router must not run"))
        runner = _runner(
            state,
            agent_run=agent_run,
            router_provider_factory=router_factory,
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

            observations = state.ghost_observations.read_committed(session_id="session-1")

    agent_run.assert_not_called()
    router_factory.assert_not_called()
    assert main_provider.prompts
    assert len(main_provider.prompts) == 1
    assert state.run_registry.last_terminal_event()["mode"] == "chat"
    assert state.run_registry.last_terminal_event()["summary"] == "plain chat"
    assert len(observations) == 1


def test_auto_router_chat_route_with_project_stays_in_chat_mode() -> None:
    """Direct auto answer with a project: task_start still shows the deferred
    baseline while the terminal event carries the decided chat mode."""
    with tempfile.TemporaryDirectory() as td:
        project = Path(td, "project")
        project.mkdir()
        state = server.AppContext(Path(td, "state"))
        main_provider = _Provider("chat reply")
        agent_run = mock.Mock(return_value=RunResult("should not run", "done", 1))
        runner = _runner(
            state,
            agent_run=agent_run,
            router_provider_factory=mock.Mock(
                side_effect=AssertionError("retired router must not run")),
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

        emitted = []
        while not events.empty():
            emitted.append(events.get_nowait())

    start = next(event for event in emitted if event["type"] == "task_start")
    agent_run.assert_not_called()
    assert main_provider.prompts
    assert len(main_provider.prompts) == 1
    assert state.run_registry.last_terminal_event()["summary"] == "chat reply"
    assert start["mode"] == "agent"
    assert state.run_registry.last_terminal_event()["mode"] == "chat"


def test_manual_intent_bypasses_router() -> None:
    with tempfile.TemporaryDirectory() as td:
        state = server.AppContext(td)
        router_factory = mock.Mock(return_value=_Provider('{"mode":"chat","confidence":1}'))
        runner = _runner(state, router_provider_factory=router_factory)
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

    router_factory.assert_not_called()
    assert state.run_registry.last_terminal_event()["mode"] == "research"
    assert research_iteration.call_count == 1


def test_router_failure_falls_back_to_existing_baseline() -> None:
    """A dead auto first call fails open into the deterministic baseline
    runner instead of failing the task."""
    with tempfile.TemporaryDirectory() as td:
        project = Path(td, "project")
        project.mkdir()
        state = server.AppContext(Path(td, "state"))
        agent_run = mock.Mock(return_value=RunResult("fixed", "done", 1))
        runner = _runner(
            state,
            agent_run=agent_run,
            router_provider_factory=mock.Mock(
                side_effect=AssertionError("retired router must not run")),
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

    assert agent_run.call_args.args[0].permission_profile == "coding_writer"
    assert state.run_registry.last_terminal_event()["stop_reason"] == "done"


def test_router_cancellation_stops_task_without_running_baseline() -> None:
    with tempfile.TemporaryDirectory() as td:
        project = Path(td, "project")
        project.mkdir()
        state = server.AppContext(Path(td, "state"))
        agent_run = mock.Mock(return_value=RunResult("fixed", "done", 1))
        main_provider = _CancelProvider("{}")
        runner = _runner(
            state,
            agent_run=agent_run,
            router_provider_factory=mock.Mock(
                side_effect=AssertionError("retired router must not run")),
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
    assert state.run_registry.last_terminal_event()["stop_reason"] == "stopped"
    assert state.run_registry.last_terminal_event()["mode"] == "agent"
    assert main_provider.closed


def test_router_control_teach_cancellation_stops_task_without_running_baseline() -> None:
    with tempfile.TemporaryDirectory() as td:
        project = Path(td, "project")
        project.mkdir()
        state = server.AppContext(Path(td, "state"))
        agent_run = mock.Mock(return_value=RunResult("fixed", "done", 1))
        main_provider = _TeachCancelProvider("{}")
        runner = _runner(
            state,
            agent_run=agent_run,
            router_provider_factory=mock.Mock(
                side_effect=AssertionError("retired router must not run")),
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
    assert state.run_registry.last_terminal_event()["stop_reason"] == "stopped"
    assert state.run_registry.last_terminal_event()["mode"] == "agent"
    assert main_provider.closed


def test_ghost_disable_skips_auto_router_provider_call() -> None:
    """Ghost updates disabled: unified auto still answers (execution is not
    learning) but no experience observation is recorded."""
    with tempfile.TemporaryDirectory() as td:
        project = Path(td, "project")
        project.mkdir()
        state = server.AppContext(Path(td, "state"))
        assert state.ghost_inbox is not None
        state.ghost_inbox.set_learning_enabled(False)
        router_factory = mock.Mock(
            side_effect=AssertionError("retired router must not run"))
        main_provider = _Provider("fixed by chat")
        agent_run = mock.Mock(return_value=RunResult("fixed", "done", 1))
        runner = _runner(
            state,
            agent_run=agent_run,
            router_provider_factory=router_factory,
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

    router_factory.assert_not_called()
    agent_run.assert_not_called()
    assert state.run_registry.last_terminal_event()["mode"] == "chat"
    assert state.run_registry.last_terminal_event()["summary"] == "fixed by chat"
    assert state.ghost_observations.read_committed(session_id="session-1") == ()


def test_review_only_route_does_not_start_writer_or_repair() -> None:
    """Unified auto review: the first normal call requests the review action;
    the writer never starts and no repair runs."""
    with tempfile.TemporaryDirectory() as td:
        project = Path(td, "project")
        project.mkdir()
        state = server.AppContext(Path(td, "state"))
        main_provider = _Provider("ACTION: review\nPLAN: 只列 findings 不修改")
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
            router_provider_factory=mock.Mock(
                side_effect=AssertionError("retired router must not run")),
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
                ),
            )

    agent_run.assert_not_called()
    run_review.assert_called_once()
    assert state.run_registry.last_terminal_event()["mode"] == "review"
    assert state.run_registry.last_terminal_event()["changed"] is False
    assert "Bug remains" in state.run_registry.last_terminal_event()["summary"]
    assert getattr(state, "_project_writer_leases", {}) == {}


def test_review_only_provider_failure_is_reported_without_error() -> None:
    with tempfile.TemporaryDirectory() as td:
        project = Path(td, "project")
        project.mkdir()
        state = server.AppContext(Path(td, "state"))
        main_provider = _Provider("ACTION: review\nPLAN: review 不要修改")
        run_review = mock.Mock(side_effect=RuntimeError("review provider down"))
        runner = _runner(
            state,
            collect_changes=_reviewable_changes,
            run_review=run_review,
            router_provider_factory=mock.Mock(
                side_effect=AssertionError("retired router must not run")),
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
                ),
            )

    run_review.assert_called_once()
    assert state.run_registry.last_terminal_event()["stop_reason"] == "done"
    assert state.run_registry.last_terminal_event()["mode"] == "review"
    assert state.run_registry.last_terminal_event()["changed"] is False
    assert state.run_registry.last_terminal_event()["summary"] == "Review unavailable. No files were changed."


def test_review_only_uses_snapshot_diff_for_non_git_project() -> None:
    with tempfile.TemporaryDirectory() as td:
        project = Path(td, "project")
        project.mkdir()
        target = project / "app.py"
        target.write_text("old\n", encoding="utf-8")
        state = server.AppContext(Path(td, "state"))
        tracker = state.change_tracker_for(project, persistent=True)
        tracker.capture_before("app.py")
        target.write_text("new\n", encoding="utf-8")
        tracker.capture_after("app.py")
        main_provider = _Provider("ACTION: review\nPLAN: review 不要修改")
        review = ReviewResult("approved", "Looks good", [])
        run_review = mock.Mock(return_value=("glm", review))
        runner = _runner(
            state,
            collect_changes=collect_project_changes,
            run_review=run_review,
            router_provider_factory=mock.Mock(
                side_effect=AssertionError("retired router must not run")),
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
                ),
            )

    assert state.run_registry.last_terminal_event()["mode"] == "review"
    assert state.run_registry.last_terminal_event()["changed"] is False
    assert run_review.call_args.kwargs["changes"]["mode"] == "snapshot"
