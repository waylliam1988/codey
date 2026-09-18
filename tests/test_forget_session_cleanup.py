"""forget_conversation must clear executable pending state per session."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest import mock

from codey.app import server as server_module


class ForgetSessionCleanupTests(unittest.TestCase):
    def test_forget_clears_session_approvals_and_research_changes(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            state = server_module.AppContext(Path(td) / "state")
            run = state.reserve_run(
                session_id="session-1",
                project=None,
                task="hello",
                provider_id="deepseek",
            )
            assert run is not None
            state.add_pending_shell_approval("shell-1", {
                "id": "shell-1",
                "session_id": "session-1",
                "run_id": run.run_id,
                "command": "pytest -q",
                "cwd": ".",
                "project": None,
            })
            state.record_research_changes(run.run_id, object())
            # Another session's executable state must survive.
            state.add_pending_shell_approval("shell-2", {
                "id": "shell-2",
                "session_id": "session-2",
                "run_id": "run-other",
                "command": "ruff",
                "cwd": ".",
                "project": None,
            })
            state.research_changes["run-other"] = object()
            state._research_change_sessions["run-other"] = "session-2"

            failures = state.forget_conversation("session-1")

            self.assertNotIn("approvals", failures)
            self.assertEqual(set(state.pending_shell_approvals()), {"shell-2"})
            self.assertNotIn(run.run_id, state.research_changes)
            self.assertIn("run-other", state.research_changes)
            denied = state.run_registry.last_shell_result()
            assert denied is not None
            self.assertEqual(denied["session_id"], "session-1")
            self.assertFalse(denied["approved"])

    def test_conversation_failure_does_not_short_circuit_cleanup(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            state = server_module.AppContext(Path(td) / "state")
            run = state.reserve_run(
                session_id="session-1",
                project=None,
                task="hello",
                provider_id="deepseek",
            )
            assert run is not None
            state.add_pending_shell_approval("shell-1", {
                "id": "shell-1",
                "session_id": "session-1",
                "run_id": run.run_id,
                "command": "pytest -q",
                "cwd": ".",
                "project": None,
            })
            state.record_research_changes(run.run_id, object())

            with mock.patch.object(
                state.conversation_registry,
                "forget",
                side_effect=PermissionError("denied"),
            ):
                failures = state.forget_conversation("session-1")

            self.assertIn("conversation", failures)
            self.assertEqual(state.pending_shell_approvals(), {})
            self.assertNotIn(run.run_id, state.research_changes)
            denied = state.run_registry.last_shell_result()
            assert denied is not None
            self.assertFalse(denied["approved"])


if __name__ == "__main__":
    unittest.main()
