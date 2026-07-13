from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest import mock

from codey import server
from codey.agent import RunResult
from codey.events import RunEvent
from codey.models import ToolCall
from codey.tool_runtime import ToolOutcome


class WorkCheckpointFlowTests(unittest.TestCase):
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
            state = server.State(root / "state")
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
                mock.patch.object(server, "_run_project_audit", return_value=()),
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
                mock.patch.object(server, "_run_project_audit", return_value=()),
                mock.patch.object(server, "_run_review", return_value=None),
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

    def test_unrelated_new_task_does_not_receive_old_checkpoint(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            project = root / "project"
            project.mkdir()
            (project / "app.py").write_text("content\n", encoding="utf-8")
            state = server.State(root / "state")
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
                mock.patch.object(server, "_run_project_audit", return_value=()),
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
            state = server.State(root / "state")
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
                mock.patch.object(server, "_run_project_audit", return_value=()),
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
            state = server.State(root / "state")
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
                    ToolCall("run", {"path": ".", "command": "python -m pytest tests/test_auth.py"}),
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
                mock.patch.object(server, "_run_project_audit", return_value=()),
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
            self.assertIn("python -m pytest tests/test_auth.py", observed)
            self.assertNotIn("python -m pytest tests/old.py", observed)
            self.assertIn("Latest edit epoch: 1", execution)
            self.assertIn("python -m pytest tests/test_auth.py", execution)
            self.assertNotIn("python -m pytest tests/old.py", execution)
            facts = state.project_facts.load(project)
            self.assertEqual(
                facts.successful_changes[-1].checks,
                ("python -m pytest tests/test_auth.py",),
            )

    def test_verification_map_failure_does_not_block_review(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            project = root / "project"
            project.mkdir()
            (project / "app.py").write_text("before\n", encoding="utf-8")
            state = server.State(root / "state")
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
                mock.patch.object(server, "_run_project_audit", return_value=()),
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
            state = server.State(root / "state")
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
                mock.patch.object(server, "_run_project_audit", return_value=()),
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
                mock.patch.object(server, "_run_project_audit", return_value=()),
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
            state = server.State(root / "state")
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
                mock.patch.object(server, "_run_project_audit", return_value=()),
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
            state = server.State(root / "state")
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
                mock.patch.object(server, "_run_project_audit", return_value=()),
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
            state = server.State(root / "state")
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
                mock.patch.object(server, "_run_project_audit", return_value=()),
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
