from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from codey.runs.work_checkpoint import (
    MAX_CHECKPOINT_BYTES,
    MAX_CHANGED_FILES,
    MAX_COMMAND_CHARS,
    MAX_REL_PATH_CHARS,
    MAX_STOP_REASON_CHARS,
    MAX_SUCCESSFUL_CHECKS,
    MAX_TASK_CHARS,
    MAX_WORK_CHECKPOINT_PROMPT_CHARS,
    CheckpointCheck,
    CheckpointFile,
    WorkCheckpoint,
    WorkCheckpointStore,
    render_work_checkpoint,
)

FINGERPRINT = "sha256:" + ("1" * 64)


def _max_len_rel_path(index: int) -> str:
    prefix = f"src/{index:02d}/"
    suffix = ".py"
    return prefix + ("p" * (MAX_REL_PATH_CHARS - len(prefix) - len(suffix))) + suffix


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
            item = store.record_run(
                item,
                command="python -m unittest",
                cwd=".",
                ok=True,
                workspace_revision=1,
                workspace_fingerprint=FINGERPRINT,
            )
            self.assertEqual(len(item.successful_checks_after_last_change), 1)

            item = store.record_edit(item, "app.py")

            self.assertEqual(item.successful_checks_after_last_change, ())
            self.assertEqual(item.changed_files[0].path, "app.py")
            self.assertTrue(item.changed_files[0].after_hash.startswith("sha256:"))
            self.assertNotIn("one", store.path_for("session-1").read_text(encoding="utf-8"))

    def test_unhashable_edit_stays_visible_in_the_checkpoint(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            home = Path(td) / "state"
            project = Path(td) / "project"
            project.mkdir()
            store = WorkCheckpointStore(home)
            item = store.start(run_id="run-1", session_id="session-1", project=project, task="Change app")

            # A directory cannot be hashed; the edit must still be recorded
            # visibly instead of silently dropping the changed path.
            (project / "weird").mkdir()
            item = store.record_edit(item, "weird")

            self.assertEqual(item.changed_files, ())
            self.assertEqual(item.hash_unavailable_files, ("weird",))
            rendered = render_work_checkpoint(item)
            self.assertIn("unverified", rendered)
            self.assertIn("weird", rendered)

            reloaded = WorkCheckpointStore(home).load("session-1")
            self.assertIsNotNone(reloaded)
            assert reloaded is not None
            self.assertEqual(reloaded.hash_unavailable_files, ("weird",))

            # Reconcile stays conservative: an unverifiable file marks the
            # workspace as changed so inherited checks are invalidated.
            reconciled = store.reconcile(reloaded)
            self.assertTrue(reconciled.workspace_changed)

            # Once a later edit captures the hash, the marker clears.
            target = project / "app.py"
            target.write_text("new\n", encoding="utf-8")
            item = store.record_edit(item, "app.py")
            item = store.record_edit(item, "weird")
            self.assertEqual(item.hash_unavailable_files, ("weird",))
            self.assertEqual(len(item.changed_files), 1)

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
                workspace_revision=1,
                workspace_fingerprint=FINGERPRINT,
            )

            item = store.record_edit(item, "sub/../app.py")

            self.assertEqual(item.successful_checks_after_last_change, ())
            self.assertEqual([entry.path for entry in item.changed_files], ["app.py"])

            item = store.record_run(
                item,
                command="python -m unittest",
                cwd=".",
                ok=True,
                workspace_revision=1,
                workspace_fingerprint=FINGERPRINT,
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
                workspace_revision=1,
                workspace_fingerprint=FINGERPRINT,
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
                workspace_revision=1,
                workspace_fingerprint=FINGERPRINT,
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

            item = store.record_run(
                item,
                command="python -m pytest",
                cwd="tests",
                ok=False,
                workspace_revision=1,
                workspace_fingerprint=FINGERPRINT,
            )

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
                workspace_revision=1,
                workspace_fingerprint=FINGERPRINT,
            )

            item = store.record_run(
                item,
                command="python -m pytest",
                cwd=".",
                ok=False,
                workspace_revision=1,
                workspace_fingerprint=FINGERPRINT,
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
            item = store.record_run(
                item,
                command="python -m unittest",
                cwd=".",
                ok=True,
                workspace_revision=1,
                workspace_fingerprint=FINGERPRINT,
            )
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

    def test_legacy_checkpoint_check_without_workspace_revision_is_not_inherited(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            project = Path(td) / "project"
            project.mkdir()
            store = WorkCheckpointStore(Path(td) / "state")
            path = store.path_for("s")
            path.parent.mkdir(parents=True)
            path.write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "run_id": "r",
                        "session_id": "s",
                        "project": str(project),
                        "original_task": "task",
                        "status": "interrupted",
                        "started_at": "2026-01-01T00:00:00Z",
                        "updated_at": "2026-01-01T00:00:00Z",
                        "changed_files": [],
                        "hash_unavailable_files": [],
                        "successful_checks_after_last_change": [
                            {"command": "python -m pytest", "cwd": "."}
                        ],
                        "stop_reason": "no_progress",
                    }
                ),
                encoding="utf-8",
            )

            item = store.load("s")

        self.assertIsNotNone(item)
        assert item is not None
        self.assertEqual(item.successful_checks_after_last_change, ())

    def test_render_contains_only_bounded_execution_facts(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            project = Path(td) / "project"
            project.mkdir()
            (project / "app.py").write_text("secret source body", encoding="utf-8")
            store = WorkCheckpointStore(Path(td) / "state")
            item = store.start(run_id="r", session_id="s", project=project, task="Fix app")
            item = store.record_edit(item, "app.py")
            item = store.record_run(
                item,
                command="python -m unittest",
                cwd=".",
                ok=True,
                workspace_revision=1,
                workspace_fingerprint=FINGERPRINT,
            )

            rendered = render_work_checkpoint(item)

            self.assertIn("app.py", rendered)
            self.assertIn("python -m unittest", rendered)
            self.assertNotIn(str(project), rendered)
            self.assertNotIn("secret source body", rendered)
            self.assertNotIn("remaining", rendered.lower())

    def test_rendered_checkpoint_fits_prompt_budget_contract(self) -> None:
        files = tuple(
            CheckpointFile(_max_len_rel_path(index), "sha256:" + ("0" * 64))
            for index in range(MAX_CHANGED_FILES)
        )
        checks = tuple(
            CheckpointCheck(
                "x" * MAX_COMMAND_CHARS,
                _max_len_rel_path(index),
                1,
                FINGERPRINT,
            )
            for index in range(MAX_SUCCESSFUL_CHECKS)
        )
        item = WorkCheckpoint(
            run_id="r",
            session_id="s",
            project="E:/project",
            original_task="T" * MAX_TASK_CHARS,
            status="ready_for_review",
            changed_files=files,
            successful_checks_after_last_change=checks,
            stop_reason="S" * MAX_STOP_REASON_CHARS,
            workspace_changed=True,
        )

        rendered = render_work_checkpoint(item)

        self.assertLessEqual(len(rendered), MAX_WORK_CHECKPOINT_PROMPT_CHARS)
        self.assertIn(files[0].path, rendered)
        self.assertIn(files[len(files) // 2].path, rendered)
        self.assertIn(files[-1].path, rendered)
        self.assertIn(checks[-1].command, rendered)


if __name__ == "__main__":
    unittest.main()
