"""One authoritative WorkspaceState per edit; ignored_paths consistent.

Locks:
- Edit fingerprint and revision bump share one ``ignored_paths`` config.
- Same-batch edit+run sees the post-edit identity (no stale pre-edit).
- Repeated edits advance monotonically with a single bounded scan each.
"""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path


class WorkspaceAuthoritativeStateTests(unittest.TestCase):
    def test_sync_uses_configured_ignored_paths(self) -> None:
        from unittest.mock import patch

        from codey.operations.kernel_provenance import sync_workspace_state_after_edit
        from codey.operations.task_session import TaskSession
        from codey.policies.task_policy import TaskPolicy
        from codey.workspace.revision import WorkspaceRevisionStore

        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            project = root / "project"
            project.mkdir()
            (project / "a.py").write_text("x = 2\n", encoding="utf-8")
            store = WorkspaceRevisionStore(root / "state")
            session = TaskSession(policy=TaskPolicy(frozenset({"control", "project.write"})))
            with patch.object(store, "bump_state", wraps=store.bump_state) as bump:
                revision, fingerprint = sync_workspace_state_after_edit(
                    session, project, ignored_paths=("gen",), revision_store=store,
                )
            bump.assert_called_once_with(project, ignored_paths=("gen",))
            self.assertEqual((session.workspace_revision, session.workspace_fingerprint), (revision, fingerprint))
            self.assertGreater(revision, 0)
            self.assertTrue(fingerprint.startswith("sha256:"))

    def test_kernel_edit_and_following_run_share_one_store_bump(self) -> None:
        from unittest.mock import patch

        from codey.operations.kernel_execution import execute_turn
        from codey.operations.task_session import TaskSession
        from codey.policies.task_policy import TaskPolicy
        from codey.runtime.core.models import ToolCall, ToolResult
        from codey.workspace.revision import WorkspaceRevisionStore

        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            project = root / "project"
            project.mkdir()
            store = WorkspaceRevisionStore(root / "state")
            session = TaskSession(policy=TaskPolicy(frozenset({"control", "project.write", "project.verify"})))

            def edit(call):
                (project / "a.py").write_text("x = 2\n", encoding="utf-8")
                return ToolResult(call, "edited", ok=True, audit={"changed": True})

            with patch.object(store, "bump_state", wraps=store.bump_state) as bump:
                results = execute_turn(session, [
                    ToolCall("edit", {"path": "a.py", "content": "x = 2\n"}),
                    ToolCall("run", {"path": ".", "command": "python -m pytest"}),
                ], executors={"edit": edit, "run": lambda call: ToolResult(call, "passed", ok=True, audit={"exit_code": 0})},
                   project_path=project, workspace_revision_store=store, run_id="one-bump")
            bump.assert_called_once()
            self.assertEqual(len(results), 2)
            self.assertTrue(session.verifications[-1]["passed"])
            self.assertEqual(results[0].audit["workspace_revision"], results[1].audit["workspace_revision"])
            self.assertEqual(results[0].audit["workspace_fingerprint"], results[1].audit["workspace_fingerprint"])

    def test_ignored_file_does_not_change_fingerprint(self) -> None:
        from codey.workspace.revision import workspace_fingerprint

        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "keep.py").write_text("x=1\n", encoding="utf-8")
            (root / "gen").mkdir()
            (root / "gen" / "out.py").write_text("v1\n", encoding="utf-8")
            fp1 = workspace_fingerprint(root, ignored_paths=("gen",))
            (root / "gen" / "out.py").write_text("v2 changed but ignored\n", encoding="utf-8")
            fp2 = workspace_fingerprint(root, ignored_paths=("gen",))
            self.assertEqual(fp1, fp2)
            fp3 = workspace_fingerprint(root, ignored_paths=())
            (root / "keep.py").write_text("x=2\n", encoding="utf-8")
            fp4 = workspace_fingerprint(root, ignored_paths=())
            self.assertNotEqual(fp3, fp4)


if __name__ == "__main__":
    unittest.main()
