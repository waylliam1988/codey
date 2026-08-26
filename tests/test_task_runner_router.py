from __future__ import annotations

from pathlib import Path
import tempfile
from unittest import mock

from codey.runtime import cancellation
from codey.providers import controls as provider_controls
from codey.agents.runner import RunResult
from codey.workspace.changes import collect_changes as collect_project_changes
from codey.research.pipeline import ResearchIterationRun
from codey.research.runner import ResearchRunResult
from codey.reviews.core import ReviewFinding, ReviewResult
from codey.app import server
from codey.app.task_runner import TaskRequest, TaskRunner


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
    state: server.State,
    *,
    agent_run=None,
    collect_changes=_empty_changes,
    run_review=None,
    router_provider_factory=None,
    is_git_repository=None,
) -> TaskRunner:
    return TaskRunner(
        state,
        agent_run=agent_run or mock.Mock(return_value=RunResult("done", "done", 1)),
        collect_changes=collect_changes,
        run_review=run_review or mock.Mock(return_value=None),
        capture_provider_failure=server.capture_provider_failure,
        project_facts=state.project_facts,
        work_checkpoints=state.work_checkpoints,
        run_ledgers=state.run_ledgers,
        managed_outputs=state.managed_outputs,
        knowledge_store=state.knowledge_store,
        is_git_repository=is_git_repository or (lambda _project: True),
        ghost_router_provider_factory=router_provider_factory,
    )


def _done_events(state: server.State) -> list[dict]:
    return list(state.last_terminal_event and [state.last_terminal_event] or [])


def _run_and_wait_for_local_maintenance(
    runner: TaskRunner,
    state: server.State,
    request: TaskRequest,
) -> None:
    runner.run(request)
    state.wait_for_ghost_sleep(timeout=2)


def test_auto_router_result_is_consumed_before_task_start_and_main_connect() -> None:
    with tempfile.TemporaryDirectory() as td:
        state = server.State(td)
        events = state.subscribe()
        main_provider = _Provider()
        route_provider = _Provider('{"mode":"research","confidence":0.91,"reason":"fresh info"}')
        order: list[str] = []

        def router_factory(_provider_id: str):
            order.append("router")
            return route_provider

        def get_provider(_provider_id: str):
            order.append("main")
            return main_provider

        runner = _runner(state, router_provider_factory=router_factory)
        runner._run_research_iteration = mock.Mock(
            return_value=ResearchIterationRun(
                result=ResearchRunResult("q", "researched", "done", 1)
            )
        )

        with mock.patch.object(state, "get_provider", side_effect=get_provider):
            _run_and_wait_for_local_maintenance(
                runner,
                state,
                TaskRequest("session-1", None, "查一下今天的版本变化", 8, False, "deepseek"),
            )

        emitted = []
        while not events.empty():
            emitted.append(events.get_nowait())

    start = next(event for event in emitted if event["type"] == "task_start")
    done = _done_events(state)[0]
    assert order[:2] == ["router", "main"]
    assert start["mode"] == "research"
    assert done["mode"] == "research"
    assert runner._run_research_iteration.call_count == 1
    assert route_provider.new_chat_called
    assert route_provider.new_chat_timeout == 8.0
    assert route_provider.send_timeout == 12.0
    assert "Ghost" not in route_provider.prompts[0]
    assert "Codey" not in route_provider.prompts[0]


def test_auto_router_hard_rule_blocks_writer_when_user_says_not_to_edit() -> None:
    with tempfile.TemporaryDirectory() as td:
        project = Path(td, "project")
        project.mkdir()
        state = server.State(Path(td, "state"))
        route_provider = _Provider('{"mode":"project_writer","confidence":0.96,"reason":"project"}')
        agent_run = mock.Mock(return_value=RunResult("plan", "done", 1))
        runner = _runner(
            state,
            agent_run=agent_run,
            router_provider_factory=lambda _provider_id: route_provider,
        )

        with mock.patch.object(state, "get_provider", return_value=_Provider()):
            _run_and_wait_for_local_maintenance(
                runner,
                state,
                TaskRequest(
                    "session-1",
                    str(project),
                    "先别改代码，只给我一个修改方案",
                    8,
                    False,
                    "deepseek",
                ),
            )

    assert agent_run.call_args.kwargs["permission_profile"] == "planning_readonly"
    assert agent_run.call_args.kwargs["change_tracker"] is None
    assert state.last_terminal_event["mode"] == "planning"


def test_auto_router_hard_rule_blocks_project_access_when_user_forbids_files() -> None:
    with tempfile.TemporaryDirectory() as td:
        project = Path(td, "project")
        project.mkdir()
        state = server.State(Path(td, "state"))
        main_provider = _Provider("plain chat")
        route_provider = _Provider('{"mode":"project_writer","confidence":0.96,"reason":"project"}')
        agent_run = mock.Mock(return_value=RunResult("should not run", "done", 1))
        runner = _runner(
            state,
            agent_run=agent_run,
            router_provider_factory=lambda _provider_id: route_provider,
        )

        with mock.patch.object(state, "get_provider", return_value=main_provider):
            _run_and_wait_for_local_maintenance(
                runner,
                state,
                TaskRequest(
                    "session-1",
                    str(project),
                    "不要读写项目文件，只普通聊一下这个想法。",
                    8,
                    False,
                    "deepseek",
                ),
            )
        exported = state.ghost_router.export_state()

    agent_run.assert_not_called()
    assert main_provider.prompts
    assert state.last_terminal_event["mode"] == "chat"
    assert state.last_terminal_event["summary"] == "plain chat"
    assert exported["router"]["records"][-1]["selected_mode"] == "project"
    assert exported["router"]["records"][-1]["final_mode"] == "chat"
    assert exported["router"]["records"][-1]["skipped_reason"] == "project_access_forbidden"


def test_auto_router_chat_route_with_project_stays_in_chat_mode() -> None:
    with tempfile.TemporaryDirectory() as td:
        project = Path(td, "project")
        project.mkdir()
        state = server.State(Path(td, "state"))
        main_provider = _Provider("chat reply")
        route_provider = _Provider('{"mode":"chat","confidence":0.96,"reason":"general chat"}')
        agent_run = mock.Mock(return_value=RunResult("should not run", "done", 1))
        runner = _runner(
            state,
            agent_run=agent_run,
            router_provider_factory=lambda _provider_id: route_provider,
        )
        events = state.subscribe()

        with mock.patch.object(state, "get_provider", return_value=main_provider):
            _run_and_wait_for_local_maintenance(
                runner,
                state,
                TaskRequest(
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
    assert state.last_terminal_event["summary"] == "chat reply"
    assert start["mode"] == "chat"
    assert state.last_terminal_event["mode"] == "chat"


def test_manual_intent_bypasses_router() -> None:
    with tempfile.TemporaryDirectory() as td:
        state = server.State(td)
        router_factory = mock.Mock(return_value=_Provider('{"mode":"chat","confidence":1}'))
        runner = _runner(state, router_provider_factory=router_factory)
        runner._run_research_iteration = mock.Mock(
            return_value=ResearchIterationRun(
                result=ResearchRunResult("q", "manual research", "done", 1)
            )
        )

        with mock.patch.object(state, "get_provider", return_value=_Provider()):
            _run_and_wait_for_local_maintenance(
                runner,
                state,
                TaskRequest(
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
    assert state.last_terminal_event["mode"] == "research"
    assert runner._run_research_iteration.call_count == 1


def test_router_failure_falls_back_to_existing_baseline() -> None:
    with tempfile.TemporaryDirectory() as td:
        project = Path(td, "project")
        project.mkdir()
        state = server.State(Path(td, "state"))
        agent_run = mock.Mock(return_value=RunResult("fixed", "done", 1))
        runner = _runner(
            state,
            agent_run=agent_run,
            router_provider_factory=mock.Mock(side_effect=RuntimeError("offline")),
        )

        with mock.patch.object(state, "get_provider", return_value=_Provider()):
            _run_and_wait_for_local_maintenance(
                runner,
                state,
                TaskRequest(
                    "session-1",
                    str(project),
                    "修复测试",
                    8,
                    False,
                    "deepseek",
                ),
            )

        exported = state.ghost_router.export_state()

    assert agent_run.call_args.kwargs["permission_profile"] == "coding_writer"
    assert state.last_terminal_event["stop_reason"] == "done"
    assert exported["router"]["records"][-1]["skipped_reason"] == "router_error"


def test_router_cancellation_stops_task_without_running_baseline() -> None:
    with tempfile.TemporaryDirectory() as td:
        project = Path(td, "project")
        project.mkdir()
        state = server.State(Path(td, "state"))
        agent_run = mock.Mock(return_value=RunResult("fixed", "done", 1))
        route_provider = _CancelProvider("{}")
        runner = _runner(
            state,
            agent_run=agent_run,
            router_provider_factory=mock.Mock(return_value=route_provider),
        )

        with mock.patch.object(state, "get_provider", return_value=_Provider()):
            runner.run(TaskRequest(
                "session-1",
                str(project),
                "修复测试",
                8,
                False,
                "deepseek",
            ))

    agent_run.assert_not_called()
    assert state.last_terminal_event["stop_reason"] == "stopped"
    assert state.last_terminal_event["mode"] == "agent"
    assert route_provider.closed


def test_router_control_teach_cancellation_stops_task_without_running_baseline() -> None:
    with tempfile.TemporaryDirectory() as td:
        project = Path(td, "project")
        project.mkdir()
        state = server.State(Path(td, "state"))
        agent_run = mock.Mock(return_value=RunResult("fixed", "done", 1))
        route_provider = _TeachCancelProvider("{}")
        runner = _runner(
            state,
            agent_run=agent_run,
            router_provider_factory=mock.Mock(return_value=route_provider),
        )

        with mock.patch.object(state, "get_provider", return_value=_Provider()):
            runner.run(TaskRequest(
                "session-1",
                str(project),
                "修复测试",
                8,
                False,
                "deepseek",
            ))

    agent_run.assert_not_called()
    assert state.last_terminal_event["stop_reason"] == "stopped"
    assert state.last_terminal_event["mode"] == "agent"
    assert route_provider.closed


def test_ghost_disable_skips_auto_router_provider_call() -> None:
    with tempfile.TemporaryDirectory() as td:
        project = Path(td, "project")
        project.mkdir()
        state = server.State(Path(td, "state"))
        assert state.ghost_inbox is not None
        state.ghost_inbox.set_learning_enabled(False)
        router_factory = mock.Mock(return_value=_Provider('{"mode":"chat","confidence":1}'))
        agent_run = mock.Mock(return_value=RunResult("fixed", "done", 1))
        runner = _runner(
            state,
            agent_run=agent_run,
            router_provider_factory=router_factory,
        )

        with mock.patch.object(state, "get_provider", return_value=_Provider()):
            _run_and_wait_for_local_maintenance(
                runner,
                state,
                TaskRequest(
                    "session-1",
                    str(project),
                    "修复测试",
                    8,
                    False,
                    "deepseek",
                ),
            )

    router_factory.assert_not_called()
    assert agent_run.call_args.kwargs["permission_profile"] == "coding_writer"


def test_review_only_route_does_not_start_writer_or_repair() -> None:
    with tempfile.TemporaryDirectory() as td:
        project = Path(td, "project")
        project.mkdir()
        state = server.State(Path(td, "state"))
        route_provider = _Provider('{"mode":"review","confidence":0.94,"reason":"review diff"}')
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
            router_provider_factory=lambda _provider_id: route_provider,
        )

        with mock.patch.object(
            state,
            "get_provider",
            side_effect=AssertionError("review mode must not connect main provider"),
        ):
            _run_and_wait_for_local_maintenance(
                runner,
                state,
                TaskRequest(
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
    assert state.last_terminal_event["mode"] == "review"
    assert state.last_terminal_event["changed"] is False
    assert "Bug remains" in state.last_terminal_event["summary"]


def test_review_only_provider_failure_is_reported_without_error() -> None:
    with tempfile.TemporaryDirectory() as td:
        project = Path(td, "project")
        project.mkdir()
        state = server.State(Path(td, "state"))
        route_provider = _Provider('{"mode":"review","confidence":0.94,"reason":"review diff"}')
        run_review = mock.Mock(side_effect=RuntimeError("review provider down"))
        runner = _runner(
            state,
            collect_changes=_reviewable_changes,
            run_review=run_review,
            router_provider_factory=lambda _provider_id: route_provider,
        )

        with mock.patch.object(
            state,
            "get_provider",
            side_effect=AssertionError("review mode must not connect main provider"),
        ):
            _run_and_wait_for_local_maintenance(
                runner,
                state,
                TaskRequest(
                    "session-1",
                    str(project),
                    "review 一下这次 diff，不要修改",
                    8,
                    False,
                    "deepseek",
                ),
            )

    run_review.assert_called_once()
    assert state.last_terminal_event["stop_reason"] == "done"
    assert state.last_terminal_event["mode"] == "review"
    assert state.last_terminal_event["changed"] is False
    assert state.last_terminal_event["summary"] == "Review unavailable. No files were changed."


def test_review_only_uses_snapshot_diff_for_non_git_project() -> None:
    with tempfile.TemporaryDirectory() as td:
        project = Path(td, "project")
        project.mkdir()
        target = project / "app.py"
        target.write_text("old\n", encoding="utf-8")
        state = server.State(Path(td, "state"))
        tracker = state.change_tracker_for(project, persistent=True)
        tracker.capture_before("app.py")
        target.write_text("new\n", encoding="utf-8")
        tracker.capture_after("app.py")
        route_provider = _Provider('{"mode":"review","confidence":0.94,"reason":"review diff"}')
        review = ReviewResult("approved", "Looks good", [])
        run_review = mock.Mock(return_value=("glm", review))
        runner = _runner(
            state,
            collect_changes=collect_project_changes,
            run_review=run_review,
            router_provider_factory=lambda _provider_id: route_provider,
            is_git_repository=lambda _project: False,
        )

        with mock.patch.object(
            state,
            "get_provider",
            side_effect=AssertionError("review mode must not connect main provider"),
        ):
            _run_and_wait_for_local_maintenance(
                runner,
                state,
                TaskRequest(
                    "session-1",
                    str(project),
                    "review 一下这次 diff，不要修改",
                    8,
                    False,
                    "deepseek",
                ),
            )

    assert state.last_terminal_event["mode"] == "review"
    assert state.last_terminal_event["changed"] is False
    assert run_review.call_args.kwargs["changes"]["mode"] == "snapshot"