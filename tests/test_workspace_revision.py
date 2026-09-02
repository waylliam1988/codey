from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from codey.workspace.revision import (
    INITIAL_WORKSPACE_REVISION,
    WorkspaceRevisionCorruption,
    WorkspaceRevisionStore,
    valid_workspace_revision,
    workspace_fingerprint,
    workspace_fingerprint_ref,
    workspace_revision_ref,
)


class WorkspaceRevisionStoreTests(unittest.TestCase):
    def test_revision_starts_at_one_and_bumps_under_state_home(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            project = Path(td) / "project"
            project.mkdir()
            store = WorkspaceRevisionStore(Path(td) / "state")

            self.assertEqual(store.current(project), INITIAL_WORKSPACE_REVISION)
            self.assertEqual(store.bump(project), INITIAL_WORKSPACE_REVISION + 1)
            self.assertEqual(store.current(project), INITIAL_WORKSPACE_REVISION + 1)
            self.assertTrue(store.path_for(project).is_file())
            self.assertFalse((project / ".codey").exists())

    def test_missing_revision_read_does_not_create_state_directory(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            project = Path(td) / "project"
            project.mkdir()
            store = WorkspaceRevisionStore(Path(td) / "state")

            state = store.current_state(project)

            self.assertEqual(state.revision, INITIAL_WORKSPACE_REVISION)
            self.assertTrue(state.fingerprint.startswith("sha256:"))
            self.assertFalse(store.path_for(project).parent.exists())

    def test_existing_corrupt_revision_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            project = Path(td) / "project"
            project.mkdir()
            store = WorkspaceRevisionStore(Path(td) / "state")
            path = store.path_for(project)
            path.parent.mkdir(parents=True)
            path.write_text("{broken", encoding="utf-8")

            with self.assertRaises(WorkspaceRevisionCorruption):
                store.current(project)
            with self.assertRaises(WorkspaceRevisionCorruption):
                store.bump(project)

    def test_existing_invalid_revision_schema_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            project = Path(td) / "project"
            project.mkdir()
            store = WorkspaceRevisionStore(Path(td) / "state")
            path = store.path_for(project)
            path.parent.mkdir(parents=True)

            for payload in (
                '{"schema_version":999,"revision":100}',
                '{"schema_version":1,"revision":false}',
                "[]",
            ):
                with self.subTest(payload=payload):
                    path.write_text(payload, encoding="utf-8")
                    with self.assertRaises(WorkspaceRevisionCorruption):
                        store.current(project)

    def test_existing_oversized_revision_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            project = Path(td) / "project"
            project.mkdir()
            store = WorkspaceRevisionStore(Path(td) / "state")
            path = store.path_for(project)
            path.parent.mkdir(parents=True)
            path.write_text("x" * 9000, encoding="utf-8")

            with self.assertRaises(WorkspaceRevisionCorruption):
                store.current(project)

    def test_workspace_fingerprint_tracks_external_content_changes(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            project = Path(td) / "project"
            project.mkdir()
            target = project / "app.py"
            target.write_text("one\n", encoding="utf-8")

            before = workspace_fingerprint(project)
            target.write_text("two\n", encoding="utf-8")
            after = workspace_fingerprint(project)

        self.assertTrue(before.startswith("sha256:"))
        self.assertTrue(after.startswith("sha256:"))
        self.assertNotEqual(before, after)

    def test_workspace_fingerprint_honors_ignored_paths(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            project = Path(td) / "project"
            (project / "generated").mkdir(parents=True)
            (project / "app.py").write_text("one\n", encoding="utf-8")
            ignored = project / "generated" / "out.py"
            ignored.write_text("one\n", encoding="utf-8")

            before = workspace_fingerprint(project, ignored_paths=("generated",))
            ignored.write_text("two\n", encoding="utf-8")
            after = workspace_fingerprint(project, ignored_paths=("generated",))

        self.assertEqual(before, after)

    def test_workspace_revision_ref_is_strict(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            project = Path(td) / "project"
            project.mkdir()

            ref = workspace_revision_ref(project, 3)
            fingerprint_ref = workspace_fingerprint_ref(
                project,
                "sha256:" + ("a" * 64),
            )

        self.assertTrue(ref.startswith("workspace_revision:"))
        self.assertTrue(ref.endswith(":3"))
        self.assertTrue(fingerprint_ref.startswith("workspace_fingerprint:"))
        self.assertTrue(fingerprint_ref.endswith(":" + ("a" * 64)))
        self.assertEqual(valid_workspace_revision(True), 0)
        self.assertEqual(workspace_revision_ref(project, 0), "")
        self.assertEqual(workspace_fingerprint_ref(project, "bad"), "")


if __name__ == "__main__":
    unittest.main()
