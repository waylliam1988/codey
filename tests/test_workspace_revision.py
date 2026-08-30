from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from codey.workspace.revision import (
    INITIAL_WORKSPACE_REVISION,
    WorkspaceRevisionStore,
    valid_workspace_revision,
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

    def test_workspace_revision_ref_is_strict(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            project = Path(td) / "project"
            project.mkdir()

            ref = workspace_revision_ref(project, 3)

        self.assertTrue(ref.startswith("workspace_revision:"))
        self.assertTrue(ref.endswith(":3"))
        self.assertEqual(valid_workspace_revision(True), 0)
        self.assertEqual(workspace_revision_ref(project, 0), "")


if __name__ == "__main__":
    unittest.main()
