from __future__ import annotations

import unittest

from codey.change_set import ChangeSet


class ChangeSetTests(unittest.TestCase):
    def test_parses_git_hunks_from_changes_dict(self) -> None:
        changes = {
            "ok": True,
            "mode": "git",
            "root": "E:/demo",
            "changed_count": 1,
            "files": [{"path": "app.py", "status": "M", "additions": 2, "deletions": 1}],
            "diff": (
                "diff --git a/app.py b/app.py\n"
                "index 111..222 100644\n"
                "--- a/app.py\n"
                "+++ b/app.py\n"
                "@@ -10,7 +10,8 @@ def run():\n"
                "-old\n"
                "+new\n"
                "+extra\n"
            ),
        }

        change_set = ChangeSet.from_changes(changes)

        self.assertTrue(change_set.ok)
        self.assertTrue(change_set.has_reviewable_diff())
        self.assertEqual(change_set.changed_paths(), ("app.py",))
        self.assertEqual(change_set.files[0].hunks[0].index, 1)
        self.assertEqual(change_set.files[0].hunks[0].old_start, 10)
        self.assertEqual(change_set.files[0].hunks[0].old_lines, 7)
        self.assertEqual(change_set.files[0].hunks[0].new_start, 10)
        self.assertEqual(change_set.files[0].hunks[0].new_lines, 8)
        self.assertIn("hunk 1: @@ -10,7 +10,8 @@ def run():", change_set.render_summary())

    def test_parses_snapshot_new_file_hunk(self) -> None:
        changes = {
            "ok": True,
            "mode": "snapshot",
            "changed_count": 1,
            "files": [{"path": "new.py", "status": "A", "additions": 2, "deletions": 0}],
            "diff": (
                "diff --git a/new.py b/new.py\n"
                "--- /dev/null\n"
                "+++ b/new.py\n"
                "@@ -0,0 +1,2 @@\n"
                "+A = 1\n"
                "+B = 2\n"
            ),
        }

        change_set = ChangeSet.from_changes(changes)

        self.assertEqual(change_set.files[0].path, "new.py")
        self.assertEqual(change_set.files[0].hunks[0].old_lines, 0)
        self.assertEqual(change_set.files[0].hunks[0].new_start, 1)
        self.assertEqual(change_set.files[0].hunks[0].new_lines, 2)

    def test_normalizes_git_rename_path_and_attaches_hunks(self) -> None:
        changes = {
            "ok": True,
            "mode": "git",
            "changed_count": 1,
            "files": [
                {
                    "path": "old.py -> new.py",
                    "status": "R",
                    "additions": 1,
                    "deletions": 0,
                }
            ],
            "diff": (
                "diff --git a/old.py b/new.py\n"
                "similarity index 90%\n"
                "rename from old.py\n"
                "rename to new.py\n"
                "--- a/old.py\n"
                "+++ b/new.py\n"
                "@@ -1,1 +1,2 @@\n"
                " old\n"
                "+new\n"
            ),
        }

        change_set = ChangeSet.from_changes(changes)
        anchor = change_set.normalize_anchor("new.py", 1, 2, None)
        raw_label_anchor = change_set.normalize_anchor("old.py -> new.py", 1, 2, None)

        self.assertEqual(change_set.changed_paths(), ("new.py",))
        self.assertEqual(change_set.files[0].previous_path, "old.py")
        self.assertEqual(change_set.files[0].hunks[0].index, 1)
        self.assertIn("- R new.py +1 -0", change_set.render_summary())
        self.assertIn("hunk 1: @@ -1,1 +1,2 @@", change_set.render_summary())
        self.assertEqual(anchor.hunk_index, 1)
        self.assertEqual(anchor.new_line, 2)
        self.assertEqual(raw_label_anchor.hunk_index, 1)

    def test_malformed_diff_still_counts_as_reviewable_when_raw_diff_exists(self) -> None:
        change_set = ChangeSet.from_changes({
            "ok": True,
            "changed_count": 1,
            "files": [{"path": "app.py", "status": "M"}],
            "diff": "this is not a unified diff but it is still raw diff text",
        })

        self.assertTrue(change_set.has_reviewable_diff())
        self.assertEqual(change_set.files[0].hunks, ())

    def test_summary_excludes_raw_diff_body(self) -> None:
        change_set = ChangeSet.from_changes({
            "ok": True,
            "changed_count": 1,
            "files": [{"path": "app.py"}],
            "diff": (
                "diff --git a/app.py b/app.py\n"
                "--- a/app.py\n"
                "+++ b/app.py\n"
                "@@ -1,1 +1,1 @@\n"
                "-old secret-ish source body\n"
                "+new secret-ish source body\n"
            ),
            "truncated": True,
        })

        summary = change_set.render_summary()

        self.assertIn("hunk 1: @@ -1,1 +1,1 @@", summary)
        self.assertIn("raw diff was truncated", summary)
        self.assertNotIn("secret-ish source body", summary)

    def test_normalizes_valid_anchor(self) -> None:
        change_set = ChangeSet.from_changes({
            "ok": True,
            "changed_count": 1,
            "files": [{"path": "app.py"}],
            "diff": (
                "diff --git a/app.py b/app.py\n"
                "--- a/app.py\n"
                "+++ b/app.py\n"
                "@@ -3,3 +5,4 @@\n"
                "-old\n"
                "+new\n"
            ),
        })

        anchor = change_set.normalize_anchor("app.py", 1, 6, 4)

        self.assertEqual(anchor.hunk_index, 1)
        self.assertEqual(anchor.new_line, 6)
        self.assertEqual(anchor.old_line, 4)

    def test_normalizes_anchor_by_line_when_hunk_is_omitted(self) -> None:
        change_set = ChangeSet.from_changes({
            "ok": True,
            "changed_count": 1,
            "files": [{"path": "app.py"}],
            "diff": (
                "diff --git a/app.py b/app.py\n"
                "--- a/app.py\n"
                "+++ b/app.py\n"
                "@@ -1,2 +1,2 @@\n"
                " same\n"
                "@@ -20,2 +25,2 @@\n"
                "-old\n"
                "+new\n"
            ),
        })

        anchor = change_set.normalize_anchor("app.py", None, 25, None)

        self.assertEqual(anchor.hunk_index, 2)
        self.assertEqual(anchor.new_line, 25)
        self.assertIsNone(anchor.old_line)

    def test_invalid_anchor_values_are_cleared_without_rejecting_path(self) -> None:
        change_set = ChangeSet.from_changes({
            "ok": True,
            "changed_count": 1,
            "files": [{"path": "app.py"}],
            "diff": (
                "diff --git a/app.py b/app.py\n"
                "--- a/app.py\n"
                "+++ b/app.py\n"
                "@@ -1,2 +1,2 @@\n"
                "-old\n"
                "+new\n"
            ),
        })

        missing_path = change_set.normalize_anchor("other.py", 1, 1, 1)
        bad_range = change_set.normalize_anchor("app.py", 9, 200, 300)

        self.assertIsNone(missing_path.hunk_index)
        self.assertIsNone(missing_path.new_line)
        self.assertIsNone(bad_range.hunk_index)
        self.assertIsNone(bad_range.new_line)
        self.assertIsNone(bad_range.old_line)


if __name__ == "__main__":
    unittest.main()
