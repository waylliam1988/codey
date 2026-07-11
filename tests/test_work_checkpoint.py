from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from codey.work_checkpoint import (
    MAX_CHECKPOINT_BYTES,
    WorkCheckpointStore,
    render_work_checkpoint,
)


class WorkCheckpointStoreTests(unittest.TestCase):
    def test_edit_hashes_file_and_invalidates_prior_checks(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            home = Path(td) / "state"
            project = Path(td) / "project"
            project.mkdir()
            target = project / "app.py"
            target.write_text("one\n", encoding="utf-8")
            store = WorkCheckpointStore(home)
            item = store.start(run_id="run-1", session_id="session-1", project=project, task="Change app")
            item = store.record_run(item, command="python -m unittest", cwd=".", ok=True)
            self.assertEqual(len(item.successful_checks_after_last_change), 1)

            item = store.record_edit(item, "app.py")

            self.assertEqual(item.successful_checks_after_last_change, ())
            self.assertEqual(item.changed_files[0].path, "app.py")
            self.assertTrue(item.changed_files[0].after_hash.startswith("sha256:"))
            self.assertNotIn("one", store.path_for("session-1").read_text(encoding="utf-8"))

    def test_edit_canonicalizes_runtime_accepted_paths_and_invalidates_checks(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            home = Path(td) / "state"
            project = Path(td) / "project"
            (project / "sub").mkdir(parents=True)
            target = project / "app.py"
            target.write_text("changed\n", encoding="utf-8")
            store = WorkCheckpointStore(home)
            item = store.start(
                run_id="run-1",
                session_id="session-1",
                project=project,
                task="Change app",
            )
            item = store.record_run(
                item,
                command="python -m unittest",
                cwd=".",
                ok=True,
            )

            item = store.record_edit(item, "sub/../app.py")

            self.assertEqual(item.successful_checks_after_last_change, ())
            self.assertEqual([entry.path for entry in item.changed_files], ["app.py"])

            item = store.record_run(
                item,
                command="python -m unittest",
                cwd=".",
                ok=True,
            )
            item = store.record_edit(item, str(target.resolve()))

            self.assertEqual(item.successful_checks_after_last_change, ())
            self.assertEqual([entry.path for entry in item.changed_files], ["app.py"])

    def test_successful_edit_invalidates_checks_even_when_path_cannot_be_recorded(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            project = Path(td) / "project"
            project.mkdir()
            store = WorkCheckpointStore(Path(td) / "state")
            item = store.start(run_id="r", session_id="s", project=project, task="task")
            item = store.record_run(
                item,
                command="python -m unittest",
                cwd=".",
                ok=True,
            )

            item = store.record_edit(item, "../outside.py")

            self.assertEqual(item.successful_checks_after_last_change, ())
            self.assertEqual(item.changed_files, ())
            self.assertEqual(item.last_action.tool, "edit")

    def test_canonical_edit_path_uses_same_length_limit_as_persisted_paths(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            project = Path(td) / "project"
            project.mkdir()
            store = WorkCheckpointStore(Path(td) / "state")
            item = store.start(run_id="r", session_id="s", project=project, task="task")
            item = store.record_run(
                item,
                command="python -m unittest",
                cwd=".",
                ok=True,
            )

            item = store.record_edit(item, "a" * 241)

            self.assertEqual(item.successful_checks_after_last_change, ())
            self.assertEqual(item.changed_files, ())
            self.assertEqual(store.load("s").changed_files, ())

    def test_failed_run_is_last_action_but_not_successful_check(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            project = Path(td) / "project"
            project.mkdir()
            store = WorkCheckpointStore(Path(td) / "state")
            item = store.start(run_id="r", session_id="s", project=project, task="task")

            item = store.record_run(item, command="python -m pytest", cwd="tests", ok=False)

            self.assertEqual(item.successful_checks_after_last_change, ())
            self.assertEqual(item.last_action.tool, "run")
            self.assertFalse(item.last_action.ok)
            self.assertNotIn("output", store.path_for("s").read_text(encoding="utf-8"))

    def test_failed_run_clears_earlier_success_on_same_code_state(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            project = Path(td) / "project"
            project.mkdir()
            store = WorkCheckpointStore(Path(td) / "state")
            item = store.start(run_id="r", session_id="s", project=project, task="task")
            item = store.record_run(
                item,
                command="python -m pytest tests/test_app.py",
                cwd=".",
                ok=True,
            )

            item = store.record_run(
                item,
                command="python -m pytest",
                cwd=".",
                ok=False,
            )

            self.assertEqual(item.successful_checks_after_last_change, ())
            self.assertFalse(item.last_action.ok)
            self.assertEqual(store.load("s").successful_checks_after_last_change, ())

    def test_reconcile_invalidates_checks_when_file_hash_changed(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            project = Path(td) / "project"
            project.mkdir()
            target = project / "app.py"
            target.write_text("one", encoding="utf-8")
            store = WorkCheckpointStore(Path(td) / "state")
            item = store.start(run_id="r", session_id="s", project=project, task="task")
            item = store.record_edit(item, "app.py")
            item = store.record_run(item, command="python -m unittest", cwd=".", ok=True)
            target.write_text("two", encoding="utf-8")

            item = store.reconcile(item)

            self.assertTrue(item.workspace_changed)
            self.assertEqual(item.successful_checks_after_last_change, ())
            self.assertIn("prior checks were invalidated", render_work_checkpoint(item))

    def test_status_round_trip_and_delete(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            project = Path(td) / "project"
            project.mkdir()
            store = WorkCheckpointStore(Path(td) / "state")
            item = store.start(run_id="r", session_id="s", project=project, task="task")
            item = store.set_status(item, "interrupted", "no_progress")

            loaded = store.load("s")
            self.assertEqual(loaded.status, "interrupted")
            self.assertEqual(loaded.stop_reason, "no_progress")
            store.delete("s")
            self.assertIsNone(store.load("s"))

    def test_wrong_session_old_schema_corrupt_and_oversized_are_ignored(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            store = WorkCheckpointStore(Path(td) / "state")
            path = store.path_for("s")
            path.parent.mkdir(parents=True)
            path.write_text("{broken", encoding="utf-8")
            self.assertIsNone(store.load("s"))
            path.write_text(json.dumps({"schema_version": 999}), encoding="utf-8")
            self.assertIsNone(store.load("s"))
            path.write_text("x" * (MAX_CHECKPOINT_BYTES + 1), encoding="utf-8")
            self.assertIsNone(store.load("s"))

    def test_render_contains_only_bounded_execution_facts(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            project = Path(td) / "project"
            project.mkdir()
            (project / "app.py").write_text("secret source body", encoding="utf-8")
            store = WorkCheckpointStore(Path(td) / "state")
            item = store.start(run_id="r", session_id="s", project=project, task="Fix app")
            item = store.record_edit(item, "app.py")
            item = store.record_run(item, command="python -m unittest", cwd=".", ok=True)

            rendered = render_work_checkpoint(item)

            self.assertIn("app.py", rendered)
            self.assertIn("python -m unittest", rendered)
            self.assertNotIn(str(project), rendered)
            self.assertNotIn("secret source body", rendered)
            self.assertNotIn("remaining", rendered.lower())


if __name__ == "__main__":
    unittest.main()
