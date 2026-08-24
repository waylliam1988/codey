from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest import mock

from codey import server
from codey.agent import RunResult
from codey.events import RunEvent
from codey.models import ToolCall
from codey.provider_diagnostics import ProviderActionError, ProviderFailure
from codey.tool_runtime import ToolOutcome

_POST_TASK_SIDEEFFECT_PATCHES: list[mock.Mock] = []


def setUpModule() -> None:
    """Disable post-task audit/consensus/advisor side effects for this file.

    The checkpoint flows never assert on them, but the real implementations
    add a fixed ~14s provider-timeout per done task and can reach live web
    providers. Production behavior is untouched; only these flow tests opt
    out.
    """

    for name in ("_run_project_audit", "_run_consensus", "_run_research_advisors"):
        patcher = mock.patch.object(server, name, return_value=None)
        patcher.start()
        _POST_TASK_SIDEEFFECT_PATCHES.append(patcher)


def tearDownModule() -> None:
    while _POST_TASK_SIDEEFFECT_PATCHES:
        _POST_TASK_SIDEEFFECT_PATCHES.pop().stop()


class WorkCheckpointFlowTests(unittest.TestCase):
    def _state(self, path: Path) -> server.State:
        """Checkpoint-flow State without background side effects.

        These tests exercise checkpoint/failover/recovery, not Ghost sleep
        learning or self repair. The real background hooks add a fixed
        ~15s wait per task (ghost sleep join) and may reach providers, so
        the flow tests disable them at the source.
        """

        state = server.State(path)
        state.kick_ghost_sleep = mock.Mock(return_value=False)
        state.wait_for_ghost_sleep = mock.Mock(return_value=True)
        state.kick_self_repair = mock.Mock(return_value=False)
        return state

    def _provider(self):
        provider = mock.Mock()
        provider.name = "DeepSeek Web"
        provider.location = "https://chat.deepseek.com/"
        return provider

    def test_interrupted_task_is_recovered_only_on_explicit_continue(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            project = root / "project"
            project.mkdir()
            (project / "app.py").write_text("before\n", encoding="utf-8")
            state = self._state(root / "state")
            provider = self._provider()

            def interrupted(*args, **kwargs):
                target = Path(args[1]) / "app.py"
                target.write_text("after\n", encoding="utf-8")
                kwargs["on_event"](RunEvent.tool_finished(
                    1,
                    ToolCall("edit", {"path": "app.py"}),
                    ToolOutcome("edited", True, changed=True),
                ))
                kwargs["on_event"](RunEvent.tool_finished(
                    2,
                    ToolCall("run", {"path": ".", "command": "python -m unittest"}),
                    ToolOutcome("ok", True, exit_code=0),
                ))
                return RunResult("stalled", "no_progress", 2, True, True, True)

            changes = {
                "ok": True,
                "mode": "git",
                "changed_count": 1,
                "files": [{"path": "app.py", "status": "M"}],
                "diff": "diff --git a/app.py b/app.py\n",
            }
            with (
                mock.patch.object(server, "STATE", state),
                mock.patch.object(state, "get_provider", return_value=provider),
                mock.patch.object(server, "agent_run", side_effect=interrupted),
                mock.patch.object(server, "collect_changes", return_value=changes),
            ):
                server._run_task("session-1", str(project), "Do work", 8, False, "deepseek")

            checkpoint = state.work_checkpoints.load("session-1")
            self.assertIsNotNone(checkpoint)
            self.assertEqual(checkpoint.status, "interrupted")
            self.assertEqual(checkpoint.stop_reason, "no_progress")
            self.assertEqual(checkpoint.changed_files[0].path, "app.py")
            self.assertEqual(checkpoint.successful_checks_after_last_change[0].command, "python -m unittest")

            captured = {}

            def resumed(*args, **kwargs):
                captured.update(kwargs)
                return RunResult("finished", "done", 1, False, False, False)

            with (
                mock.patch.object(server, "STATE", state),
                mock.patch.object(state, "get_provider", return_value=provider),
                mock.patch.object(server, "agent_run", side_effect=resumed),
                mock.patch.object(server, "collect_changes", return_value=changes),
                mock.patch.object(
                    server,
                    "_run_review",
                    return_value=None,
                ),
            ):
                server._run_task("session-1", str(project), "Continue safely", 8, True, "deepseek")

            self.assertIn("Local execution checkpoint", captured["work_checkpoint"])
            self.assertIn("app.py", captured["work_checkpoint"])
            self.assertEqual(captured["verification_changed_files"], ("app.py",))
            self.assertEqual(
                captured["verification_successful_checks"][0].command,
                "python -m unittest",
            )
            self.assertEqual(
                captured["verification_candidates"][0].command,
                "python -m unittest",
            )
            done = state.last_terminal_event
            self.assertTrue(done["changed"])
            self.assertTrue(done["receipt"]["checks_passed"])
            self.assertIsNone(state.work_checkpoints.load("session-1"))

    def test_provider_failure_hands_writer_to_sibling_from_checkpoint(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            project = root / "project"
            project.mkdir()
            (project / "app.py").write_text("before\n", encoding="utf-8")
            state = self._state(root / "state")
            state.provider_failover_order = lambda: ("deepseek", "stepfun", "qwen", "glm")
            first = self._provider()
            second = self._provider()
            second.name = "StepFun"
            captured = {}

            def writer(*args, **kwargs):
                if args[0] is first:
                    kwargs["change_tracker"].capture_before("app.py")
                    (project / "app.py").write_text("after\n", encoding="utf-8")
                    kwargs["on_event"](RunEvent.tool_finished(
                        2,
                        ToolCall("edit", {"path": "app.py"}),
                        ToolOutcome("edited", True, changed=True),
                    ))
                    raise ProviderActionError(ProviderFailure(
                        "DeepSeek",
                        "send",
                        "",
                        "",
                        "response missing",
                        "now",
                        "response_missing",
                    ))
                captured.update(kwargs)
                kwargs["on_event"](RunEvent.tool_finished(
                    1,
                    ToolCall("read_file", {"path": "app.py"}),
                    ToolOutcome("after", True),
                ))
                return RunResult("finished", "done", 1, False, False, False)

            with (
                mock.patch.object(server, "STATE", state),
                mock.patch.object(
                    state,
                    "get_provider",
                    side_effect=[first, second],
                ) as get_provider,
                mock.patch.object(server, "agent_run", side_effect=writer) as agent_run,
                mock.patch.object(
                    server,
                    "_run_review",
                    return_value=None,
                ) as run_review,
            ):
                server._run_task(
                    "session-takeover",
                    str(project),
                    "Update app.py",
                    8,
                    False,
                    "deepseek",
                )

            self.assertEqual(get_provider.call_args_list[1].args, ("stepfun",))
            self.assertEqual(agent_run.call_count, 2)
            self.assertTrue(captured["fresh_chat"])
            self.assertTrue(captured["strict_fresh_chat"])
            self.assertIn("Local execution checkpoint", captured["work_checkpoint"])
            self.assertIn("app.py", captured["work_checkpoint"])
            self.assertEqual(captured["verification_changed_files"], ("app.py",))
            self.assertEqual(state.last_terminal_event["provider"], "stepfun")
            self.assertEqual(state.active_run, None)
            self.assertEqual(agent_run.call_args_list[1].kwargs["max_turns"], 6)
            run_review.assert_called_once()
            done = state.last_terminal_event
            self.assertTrue(done["changed"])
            self.assertEqual(done["changes"]["files"][0]["path"], "app.py")

    def test_connect_failure_uses_next_writer_with_strict_fresh_chat(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            project = root / "project"
            project.mkdir()
            (project / "app.py").write_text("content\n", encoding="utf-8")
            state = self._state(root / "state")
            state.provider_failover_order = lambda: ("deepseek", "stepfun", "qwen", "glm")
            provider = self._provider()

            with (
                mock.patch.object(server, "STATE", state),
                mock.patch.object(
                    state,
                    "get_provider",
                    side_effect=[RuntimeError("tab unavailable"), provider],
                ) as get_provider,
                mock.patch.object(
                    server,
                    "agent_run",
                    return_value=RunResult("done", "done", 1),
                ) as agent_run,
                mock.patch.object(
                    server,
                    "collect_changes",
                    return_value={"ok": True, "changed_count": 0, "files": []},
                ),
            ):
                server._run_task(
                    "session-connect-failover",
                    str(project),
                    "Inspect app.py",
                    8,
                    False,
                    "deepseek",
                )

            self.assertEqual(
                [call.args[0] for call in get_provider.call_args_list],
                ["deepseek", "stepfun"],
            )
            self.assertEqual(agent_run.call_args.kwargs["provider_id"], "stepfun")
            self.assertTrue(agent_run.call_args.kwargs["strict_fresh_chat"])
            self.assertEqual(state.last_terminal_event["provider"], "stepfun")

    def test_first_rescue_failure_continues_to_second_sibling(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            project = root / "project"
            project.mkdir()
            (project / "app.py").write_text("content\n", encoding="utf-8")
            state = self._state(root / "state")
            state.provider_failover_order = lambda: ("deepseek", "stepfun", "qwen", "glm")
            providers = [self._provider(), self._provider(), self._provider()]
            failure = ProviderActionError(ProviderFailure(
                "web",
                "send",
                "",
                "",
                "missing",
                "now",
                "response_missing",
            ))

            with (
                mock.patch.object(server, "STATE", state),
                mock.patch.object(
                    state,
                    "get_provider",
                    side_effect=providers,
                ) as get_provider,
                mock.patch.object(
                    server,
                    "agent_run",
                    side_effect=[failure, failure, RunResult("done", "done", 1)],
                ) as agent_run,
                mock.patch.object(
                    server,
                    "collect_changes",
                    return_value={"ok": True, "changed_count": 0, "files": []},
                ),
            ):
                server._run_task(
                    "session-rescue-chain",
                    str(project),
                    "Inspect app.py",
                    8,
                    False,
                    "deepseek",
                )

            self.assertEqual(
                [call.args[0] for call in get_provider.call_args_list],
                ["deepseek", "stepfun", "qwen"],
            )
            self.assertEqual(agent_run.call_count, 3)
            self.assertEqual(state.last_terminal_event["provider"], "qwen")
            self.assertEqual(agent_run.call_args.kwargs["max_turns"], 6)

    def test_takeover_drops_green_check_when_recorded_file_hash_changed(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            project = root / "project"
            project.mkdir()
            target = project / "app.py"
            target.write_text("before\n", encoding="utf-8")
            (project / "pytest.ini").write_text("[pytest]\n", encoding="utf-8")
            state = self._state(root / "state")
            state.provider_failover_order = lambda: ("deepseek", "stepfun", "qwen", "glm")
            providers = [self._provider(), self._provider()]
            captured = {}

            def writer(*args, **kwargs):
                if args[0] is providers[0]:
                    target.write_text("after\n", encoding="utf-8")
                    kwargs["on_event"](RunEvent.tool_finished(
                        1,
                        ToolCall("edit", {"path": "app.py"}),
                        ToolOutcome("edited", True, changed=True),
                    ))
                    kwargs["on_event"](RunEvent.tool_finished(
                        2,
                        ToolCall(
                            "run",
                            {"path": ".", "command": "python -m pytest"},
                        ),
                        ToolOutcome("ok", True, exit_code=0),
                    ))
                    target.write_text("external drift\n", encoding="utf-8")
                    raise ProviderActionError(ProviderFailure(
                        "DeepSeek",
                        "send",
                        "",
                        "",
                        "missing",
                        "now",
                        "response_missing",
                    ))
                captured.update(kwargs)
                return RunResult("done", "done", 1)

            with (
                mock.patch.object(server, "STATE", state),
                mock.patch.object(state, "get_provider", side_effect=providers),
                mock.patch.object(server, "agent_run", side_effect=writer),
                mock.patch.object(
                    server,
                    "collect_changes",
                    return_value={
                        "ok": True,
                        "changed_count": 1,
                        "files": [{"path": "app.py", "status": "M"}],
                    },
                ),
                mock.patch.object(server, "_run_review", return_value=None),
            ):
                server._run_task(
                    "session-hash-takeover",
                    str(project),
                    "Update app.py",
                    8,
                    False,
                    "deepseek",
                )

            self.assertEqual(captured["verification_successful_checks"], ())
            self.assertIn(
                "Successful checks after the latest recorded change: (none)",
                captured["work_checkpoint"],
            )
            self.assertFalse(
                state.last_terminal_event["receipt"]["checks_passed"]
            )

    def test_stop_wins_over_provider_takeover(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            project = root / "project"
            project.mkdir()
            (project / "app.py").write_text("content\n", encoding="utf-8")
            state = self._state(root / "state")
            state.provider_failover_order = lambda: ("deepseek", "stepfun", "qwen", "glm")
            provider = self._provider()

            def stopped(*_args, **_kwargs):
                state.stop_flag.set()
                raise ProviderActionError(ProviderFailure(
                    "DeepSeek",
                    "send",
                    "",
                    "",
                    "uncertain",
                    "now",
                    "submission_uncertain",
                ))

            with (
                mock.patch.object(server, "STATE", state),
                mock.patch.object(
                    state,
                    "get_provider",
                    return_value=provider,
                ) as get_provider,
                mock.patch.object(server, "agent_run", side_effect=stopped),
            ):
                server._run_task(
                    "session-stop-takeover",
                    str(project),
                    "Inspect app.py",
                    8,
                    False,
                    "deepseek",
                )

            get_provider.assert_called_once_with("deepseek")
            self.assertEqual(state.last_terminal_event["stop_reason"], "stopped")

    def test_writer_takeover_stops_after_two_switches(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            project = root / "project"
            project.mkdir()
            (project / "app.py").write_text("content\n", encoding="utf-8")
            state = self._state(root / "state")
            state.provider_failover_order = lambda: ("deepseek", "stepfun", "qwen", "glm")
            providers = [self._provider(), self._provider(), self._provider()]

            def failed(*_args, **_kwargs):
                raise ProviderActionError(ProviderFailure(
                    "web",
                    "send",
                    "",
                    "",
                    "missing",
                    "now",
                    "response_missing",
                ))

            with (
                mock.patch.object(server, "STATE", state),
                mock.patch.object(
                    state,
                    "get_provider",
                    side_effect=providers,
                ) as get_provider,
                mock.patch.object(server, "agent_run", side_effect=failed) as agent_run,
            ):
                server._run_task(
                    "session-switch-limit",
                    str(project),
                    "Inspect app.py",
                    8,
                    False,
                    "deepseek",
                )

            self.assertEqual(agent_run.call_count, 3)
            self.assertEqual(get_provider.call_count, 3)
            self.assertEqual(state.last_terminal_event["stop_reason"], "error")
            self.assertEqual(state.last_terminal_event["provider"], "qwen")
            self.assertEqual(
                state.last_terminal_event["provider_failure"]["kind"],
                "response_missing",
            )

    def test_unrelated_new_task_does_not_receive_old_checkpoint(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            project = root / "project"
            project.mkdir()
            (project / "app.py").write_text("content\n", encoding="utf-8")
            state = self._state(root / "state")
            old = state.work_checkpoints.start(
                run_id="old-run",
                session_id="session-1",
                project=project,
                task="Old task",
            )
            state.work_checkpoints.set_status(old, "interrupted", "error")
            provider = self._provider()
            captured = {}

            def completed(*args, **kwargs):
                captured.update(kwargs)
                return RunResult("new answer", "done", 1, False, False, False)

            with (
                mock.patch.object(server, "STATE", state),
                mock.patch.object(state, "get_provider", return_value=provider),
                mock.patch.object(server, "agent_run", side_effect=completed),
                mock.patch.object(
                    server,
                    "collect_changes",
                    return_value={"ok": True, "changed_count": 0, "files": [], "diff": ""},
                ),
            ):
                server._run_task("session-1", str(project), "New task", 8, False, "deepseek")

            self.assertEqual(captured["work_checkpoint"], "")
            self.assertIsNone(state.work_checkpoints.load("session-1"))

    def test_successful_change_keeps_checkpoint_if_project_facts_write_fails(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            project = root / "project"
            project.mkdir()
            (project / "app.py").write_text("before\n", encoding="utf-8")
            state = self._state(root / "state")
            facts = mock.Mock()
            facts.render.return_value = ""
            facts.record_success.return_value = True
            facts.record_successful_change.return_value = False
            state.project_facts = facts
            provider = self._provider()

            def completed(*args, **kwargs):
                target = Path(args[1]) / "app.py"
                target.write_text("after\n", encoding="utf-8")
                kwargs["on_event"](RunEvent.tool_finished(
                    1,
                    ToolCall("edit", {"path": "app.py"}),
                    ToolOutcome("edited", True, changed=True),
                ))
                kwargs["on_event"](RunEvent.tool_finished(
                    2,
                    ToolCall("run", {"path": ".", "command": "python -m unittest"}),
                    ToolOutcome("ok", True, exit_code=0),
                ))
                return RunResult("finished", "done", 2, True, True, True)

            changes = {
                "ok": True,
                "mode": "git",
                "changed_count": 1,
                "files": [{"path": "app.py", "status": "M"}],
                "diff": "diff --git a/app.py b/app.py\n+after\n",
            }
            with (
                mock.patch.object(server, "STATE", state),
                mock.patch.object(state, "get_provider", return_value=provider),
                mock.patch.object(server, "agent_run", side_effect=completed),
                mock.patch.object(server, "collect_changes", return_value=changes),
                mock.patch.object(server, "_run_review", return_value=None),
            ):
                server._run_task("session-1", str(project), "Do work", 8, False, "deepseek")

            checkpoint = state.work_checkpoints.load("session-1")
            self.assertIsNotNone(checkpoint)
            self.assertEqual(checkpoint.status, "ready_for_review")
            facts.record_successful_change.assert_called_once()

    def test_review_verification_map_uses_only_checks_after_latest_edit(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            project = root / "project"
            (project / "src").mkdir(parents=True)
            (project / "tests").mkdir()
            (project / "src" / "auth.py").write_text(
                "def login():\n    return False\n",
                encoding="utf-8",
            )
            (project / "tests" / "test_auth.py").write_text(
                "from src.auth import login\n",
                encoding="utf-8",
            )
            state = self._state(root / "state")
            provider = self._provider()

            def completed(*args, **kwargs):
                on_event = kwargs["on_event"]
                on_event(RunEvent.tool_finished(
                    1,
                    ToolCall("run", {"path": ".", "command": "python -m pytest tests/old.py"}),
                    ToolOutcome("ok", True, exit_code=0),
                ))
                (project / "src" / "auth.py").write_text(
                    "def login():\n    return True\n",
                    encoding="utf-8",
                )
                on_event(RunEvent.tool_finished(
                    2,
                    ToolCall("edit", {"path": "src/auth.py"}),
                    ToolOutcome("edited", True, changed=True),
                ))
                on_event(RunEvent.tool_finished(
                    3,
                    ToolCall("run", {"path": ".", "command": "python -m pytest"}),
                    ToolOutcome("ok", True, exit_code=0),
                ))
                return RunResult("finished", "done", 3, True, True, True)

            changes = {
                "ok": True,
                "mode": "git",
                "changed_count": 1,
                "files": [{"path": "src/auth.py", "status": "M"}],
                "diff": "diff --git a/src/auth.py b/src/auth.py\n+def login():\n+    return True\n",
            }
            review = mock.Mock(return_value=None)
            with (
                mock.patch.object(server, "STATE", state),
                mock.patch.object(state, "get_provider", return_value=provider),
                mock.patch.object(server, "agent_run", side_effect=completed),
                mock.patch.object(server, "collect_changes", return_value=changes),
                mock.patch.object(server, "_run_review", review),
            ):
                server._run_task("session-1", str(project), "Fix login", 8, False, "deepseek")

            rendered = review.call_args.kwargs["verification_map"]
            execution = review.call_args.kwargs["execution_evidence"]
            self.assertIn("tests/test_auth.py", rendered)
            observed = rendered.split(
                "Observed successful checks after the latest edit:\n",
                1,
            )[1].split("Broader check candidates", 1)[0]
            self.assertIn("python -m pytest", observed)
            self.assertNotIn("python -m pytest tests/old.py", observed)
            self.assertIn("Latest edit epoch: 1", execution)
            self.assertIn("python -m pytest", execution)
            self.assertNotIn("python -m pytest tests/old.py", execution)
            facts = state.project_facts.load(project)
            self.assertEqual(
                [item.command for item in facts.successful_changes[-1].checks],
                ["python -m pytest"],
            )

    def test_verification_map_failure_does_not_block_review(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            project = root / "project"
            project.mkdir()
            (project / "app.py").write_text("before\n", encoding="utf-8")
            state = self._state(root / "state")
            provider = self._provider()
            changes = {
                "ok": True,
                "mode": "git",
                "changed_count": 1,
                "files": [{"path": "app.py", "status": "M"}],
                "diff": "diff --git a/app.py b/app.py\n+after\n",
            }
            review = mock.Mock(return_value=None)

            with (
                mock.patch.object(server, "STATE", state),
                mock.patch.object(state, "get_provider", return_value=provider),
                mock.patch.object(
                    server,
                    "agent_run",
                    return_value=RunResult("done", "done", 1, False, True, False),
                ),
                mock.patch.object(server, "collect_changes", return_value=changes),
                mock.patch.object(server, "_run_review", review),
                mock.patch(
                    "codey.task_runner.render_verification_map",
                    side_effect=RuntimeError("scan failed"),
                ),
            ):
                server._run_task("session-1", str(project), "Change app", 8, False, "deepseek")

            self.assertEqual(review.call_args.kwargs["verification_map"], "")

    def test_failed_broader_check_prevents_green_recovery_receipt(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            project = root / "project"
            project.mkdir()
            (project / "app.py").write_text("before\n", encoding="utf-8")
            state = self._state(root / "state")
            provider = self._provider()
            changes = {
                "ok": True,
                "mode": "git",
                "changed_count": 1,
                "files": [{"path": "app.py", "status": "M"}],
                "diff": "diff --git a/app.py b/app.py\n+after\n",
            }

            def interrupted(*args, **kwargs):
                on_event = kwargs["on_event"]
                (project / "app.py").write_text("after\n", encoding="utf-8")
                on_event(RunEvent.tool_finished(
                    1,
                    ToolCall("edit", {"path": "app.py"}),
                    ToolOutcome("edited", True, changed=True),
                ))
                on_event(RunEvent.tool_finished(
                    2,
                    ToolCall("run", {"path": ".", "command": "python -m pytest tests/test_app.py"}),
                    ToolOutcome("ok", True, exit_code=0),
                ))
                on_event(RunEvent.tool_finished(
                    3,
                    ToolCall("run", {"path": ".", "command": "python -m pytest"}),
                    ToolOutcome("failed", False, exit_code=1),
                ))
                return RunResult("failed broader check", "no_progress", 3, False, True, True)

            with (
                mock.patch.object(server, "STATE", state),
                mock.patch.object(state, "get_provider", return_value=provider),
                mock.patch.object(server, "agent_run", side_effect=interrupted),
                mock.patch.object(server, "collect_changes", return_value=changes),
            ):
                server._run_task("session-1", str(project), "Fix app", 8, False, "deepseek")

            checkpoint = state.work_checkpoints.load("session-1")
            self.assertEqual(checkpoint.successful_checks_after_last_change, ())

            with (
                mock.patch.object(server, "STATE", state),
                mock.patch.object(state, "get_provider", return_value=provider),
                mock.patch.object(
                    server,
                    "agent_run",
                    return_value=RunResult("done", "done", 1, False, False, False),
                ),
                mock.patch.object(server, "collect_changes", return_value=changes),
                mock.patch.object(server, "_run_review", return_value=None),
            ):
                server._run_task("session-1", str(project), "Continue", 8, True, "deepseek")

            self.assertFalse(state.last_terminal_event["receipt"]["checks_passed"])

    def test_receipt_rejects_green_command_other_than_selected_candidate(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            project = root / "project"
            project.mkdir()
            (project / "app.py").write_text("VALUE = 1\n", encoding="utf-8")
            (project / "other.py").write_text("VALUE = 0\n", encoding="utf-8")
            (project / "pytest.ini").write_text("[pytest]\n", encoding="utf-8")
            state = self._state(root / "state")
            provider = self._provider()
            changes = {
                "ok": True,
                "mode": "git",
                "changed_count": 1,
                "files": [{"path": "app.py", "status": "M"}],
                "diff": "diff --git a/app.py b/app.py\n+VALUE = 2\n",
            }

            def completed(*args, **kwargs):
                kwargs["on_event"](RunEvent.tool_finished(
                    1,
                    ToolCall("edit", {"path": "app.py"}),
                    ToolOutcome("edited", True, changed=True),
                ))
                kwargs["on_event"](RunEvent.tool_finished(
                    2,
                    ToolCall(
                        "run",
                        {"path": ".", "command": "python -m py_compile other.py"},
                    ),
                    ToolOutcome("ok", True, exit_code=0),
                ))
                return RunResult("done", "done", 2, True, True, True)

            with (
                mock.patch.object(server, "STATE", state),
                mock.patch.object(state, "get_provider", return_value=provider),
                mock.patch.object(server, "agent_run", side_effect=completed),
                mock.patch.object(server, "collect_changes", return_value=changes),
                mock.patch.object(server, "_run_review", return_value=None),
            ):
                server._run_task(
                    "session-1",
                    str(project),
                    "Update app",
                    8,
                    False,
                    "deepseek",
                )

            self.assertFalse(state.last_terminal_event["receipt"]["checks_passed"])

    def test_candidate_loader_refreshes_manifest_from_current_disk(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            project = root / "project"
            project.mkdir()
            manifest = project / "pytest.ini"
            manifest.write_text("[pytest]\n", encoding="utf-8")
            state = self._state(root / "state")
            provider = self._provider()

            def completed(*args, **kwargs):
                self.assertEqual(
                    kwargs["verification_candidates"][0].command,
                    "python -m pytest",
                )
                manifest.unlink()
                self.assertEqual(kwargs["verification_candidate_loader"](), ())
                return RunResult("done", "done", 1)

            with (
                mock.patch.object(server, "STATE", state),
                mock.patch.object(state, "get_provider", return_value=provider),
                mock.patch.object(server, "agent_run", side_effect=completed),
                mock.patch.object(
                    server,
                    "collect_changes",
                    return_value={"ok": True, "changed_count": 0, "files": []},
                ),
            ):
                server._run_task(
                    "session-1",
                    str(project),
                    "Inspect config",
                    8,
                    False,
                    "deepseek",
                )

    def test_candidate_loader_drops_stale_historical_npm_script(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            project = root / "project"
            project.mkdir()
            manifest = project / "package.json"
            manifest.write_text(
                '{"scripts":{"test":"node test.js"}}', encoding="utf-8"
            )
            state = self._state(root / "state")
            state.project_facts.record_success(project, ".", "npm test")
            provider = self._provider()

            def completed(*args, **kwargs):
                self.assertTrue(
                    any(
                        item.command == "npm test"
                        for item in kwargs["verification_candidates"]
                    )
                )
                manifest.write_text('{"scripts":{}}', encoding="utf-8")
                self.assertFalse(
                    any(
                        item.command == "npm test"
                        for item in kwargs["verification_candidate_loader"]()
                    )
                )
                return RunResult("done", "done", 1)

            with (
                mock.patch.object(server, "STATE", state),
                mock.patch.object(state, "get_provider", return_value=provider),
                mock.patch.object(server, "agent_run", side_effect=completed),
                mock.patch.object(
                    server,
                    "collect_changes",
                    return_value={"ok": True, "changed_count": 0, "files": []},
                ),
                mock.patch(
                    "codey.verification_policy.shutil.which",
                    return_value="npm",
                ),
            ):
                server._run_task(
                    "session-1",
                    str(project),
                    "Update package metadata",
                    8,
                    False,
                    "deepseek",
                )


if __name__ == "__main__":
    unittest.main()
