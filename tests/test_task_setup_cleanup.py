"""Setup-phase resource cleanup: writer/run/cancellation never leak on init failure."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest import mock

from codey.app.context import AppContext
from codey.operations import task_run as task_run_module
from codey.operations.task_run import TaskRunDeps, _setup_run_state
from codey.task.model import TaskSubmission
from codey.workspace.changes import ProjectWriteBusy


def _submission(project: str) -> TaskSubmission:
    return TaskSubmission(
        session_id="s-cleanup",
        project=project,
        task="do work",
        max_turns=2,
        continue_task=False,
        provider_id="deepseek",
        intent="project",
    )


def _deps(state: AppContext) -> TaskRunDeps:
    return TaskRunDeps(
        state=state,  # type: ignore[arg-type]
        agent_run=mock.Mock(),
        collect_changes=mock.Mock(return_value={"ok": True, "files": [], "diff": ""}),
        run_review=mock.Mock(return_value=None),
        capture_provider_failure=mock.Mock(),
        workspace_revisions=state.workspace_revisions,
        is_git_repository=lambda _p: False,
        runtime_mutations=state.runtime_mutations,
        runtime_effects=state.runtime_effects,
    )


class SetupCleanupTests(unittest.TestCase):
    def test_review_deps_failure_releases_writer_and_run(self) -> None:
        with tempfile.TemporaryDirectory() as td, tempfile.TemporaryDirectory() as state_td:
            project = str(Path(td) / "proj")
            Path(project).mkdir()
            state = AppContext(state_td)
            try:
                deps = _deps(state)
                with mock.patch.object(
                    task_run_module, "review_flow_deps", side_effect=RuntimeError("boom")
                ), self.assertRaises(RuntimeError):
                    _setup_run_state(deps, _submission(project))
                # Writer reusable, run released, cancellation restored.
                self.assertTrue(state.acquire_project_writer(project))
                state.release_project_writer(project)
                self.assertIsNone(state.run_registry.current())
                self.assertFalse(state.run_registry.stop_flag.is_set())
            finally:
                with mock.patch.object(state, "release_project_writer", lambda _p: None):
                    pass
                state.close()

    def test_ghost_deps_failure_releases_writer_and_run(self) -> None:
        with tempfile.TemporaryDirectory() as td, tempfile.TemporaryDirectory() as state_td:
            project = str(Path(td) / "proj")
            Path(project).mkdir()
            state = AppContext(state_td)
            try:
                deps = _deps(state)
                with mock.patch.object(
                    task_run_module, "ghost_task_deps", side_effect=RuntimeError("ghost boom")
                ), self.assertRaises(RuntimeError):
                    _setup_run_state(deps, _submission(project))
                self.assertTrue(state.acquire_project_writer(project))
                state.release_project_writer(project)
                self.assertIsNone(state.run_registry.current())
            finally:
                state.close()

    def test_acquire_io_error_is_not_busy(self) -> None:
        with tempfile.TemporaryDirectory() as state_td:
            state = AppContext(state_td)
            try:
                with mock.patch.object(
                    state.snapshot_store,
                    "acquire_writer",
                    side_effect=OSError("disk full"),
                ), self.assertRaises(OSError):
                    state.acquire_project_writer("/tmp/proj")
            finally:
                state.close()

    def test_setup_propagates_acquire_io_error_not_busy(self) -> None:
        with tempfile.TemporaryDirectory() as td, tempfile.TemporaryDirectory() as state_td:
            project = str(Path(td) / "proj")
            Path(project).mkdir()
            state = AppContext(state_td)
            try:
                deps = _deps(state)
                with mock.patch.object(
                    state, "acquire_project_writer", side_effect=OSError("disk full")
                ), self.assertRaises(OSError):
                    _setup_run_state(deps, _submission(project))
                self.assertIsNone(state.run_registry.current())
            finally:
                state.close()

    def test_busy_still_returns_busy_outcome(self) -> None:
        with tempfile.TemporaryDirectory() as td, tempfile.TemporaryDirectory() as state_td:
            project = str(Path(td) / "proj")
            Path(project).mkdir()
            state = AppContext(state_td)
            try:
                deps = _deps(state)
                with mock.patch.object(
                    state, "acquire_project_writer", side_effect=ProjectWriteBusy("busy")
                ):
                    setup, outcome = _setup_run_state(deps, _submission(project))
                self.assertIsNone(setup)
                self.assertIsNotNone(outcome)
                assert outcome is not None
                self.assertEqual(outcome.reason, "project_write_busy")
                self.assertIsNone(state.run_registry.current())
            finally:
                state.close()


if __name__ == "__main__":
    unittest.main()
