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


if __name__ == "__main__":
    unittest.main()
