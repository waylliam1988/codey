"""Safe review input and actual scope (batch 3)."""
from __future__ import annotations

import unittest

from codey.reviews import core as review
from codey.reviews.input import prepare_review_input


def _changes(files, diff="", truncated=False, ok=True):
    return {
        "ok": ok,
        "changed_count": len(files),
        "files": files,
        "diff": diff,
        "truncated": truncated,
    }


class ScopeTests(unittest.TestCase):
    def test_scope_matches_actual_prompt_file_list(self) -> None:
        files = [{"path": f"f{i}.py", "status": "M"} for i in range(5)]
        inp = prepare_review_input(
            project="E:/demo",
            task="fix",
            writer_summary="done",
            changes=_changes(files, diff="x\n" * 10),
        )
        for path in inp.scope.provided_files:
            self.assertIn(path, inp.prompt)
        self.assertEqual(inp.scope.total_changed_files, 5)

    def test_renderer_diff_clipping_marks_partial_scope(self) -> None:
        big_diff = "x\n" * 70000
        inp = prepare_review_input(
            project="E:/demo",
            task="fix",
            writer_summary="done",
            changes=_changes([{"path": "app.py", "status": "M"}], diff=big_diff),
        )
        self.assertTrue(inp.scope.diff_truncated)
        self.assertFalse(inp.scope.is_complete)

    def test_upstream_diff_truncation_is_preserved(self) -> None:
        inp = prepare_review_input(
            project="E:/demo",
            task="fix",
            writer_summary="done",
            changes=_changes(
                [{"path": "app.py", "status": "M"}], diff="small", truncated=True
            ),
        )
        self.assertTrue(inp.scope.diff_truncated)

    def test_file_list_limit_marks_partial_scope(self) -> None:
        files = [{"path": f"f{i}.py", "status": "M"} for i in range(30)]
        inp = prepare_review_input(
            project="E:/demo",
            task="fix",
            writer_summary="done",
            changes=_changes(files, diff="x"),
        )
        self.assertTrue(inp.scope.file_list_truncated)
        self.assertFalse(inp.scope.is_complete)

    def test_diff_for_file_outside_provided_file_list_is_not_sent(self) -> None:
        files = [{"path": f"f{i}.py", "status": "M"} for i in range(21)]
        diff = (
            "diff --git a/f20.py b/f20.py\n"
            "--- a/f20.py\n"
            "+++ b/f20.py\n"
            "@@ -1 +1 @@\n"
            "-old\n"
            "+outside = True\n"
        )
        inp = prepare_review_input(
            project="E:/demo",
            task="fix",
            writer_summary="done",
            changes=_changes(files, diff=diff),
        )
        self.assertTrue(inp.scope.file_list_truncated)
        self.assertNotIn("outside = True", inp.prompt)

    def test_collection_failure_is_not_empty_clean_scope(self) -> None:
        inp = prepare_review_input(
            project="E:/demo",
            task="fix",
            writer_summary="done",
            changes={"ok": False, "error": "git failed", "files": [], "diff": ""},
        )
        self.assertTrue(inp.scope.collection_incomplete)
        self.assertFalse(inp.scope.is_complete)

    def test_header_only_diff_still_excludes_sensitive_file(self) -> None:
        diff = (
            "--- .env\n"
            "+++ .env\n"
            "+SECRET_KEY=sk-live-abcdefghij1234567890XYZ\n"
            "--- app.py\n"
            "+++ app.py\n"
            "+safe = True\n"
        )
        inp = prepare_review_input(
            project="E:/demo",
            task="fix",
            writer_summary="done",
            changes=_changes(
                [
                    {"path": ".env", "status": "M"},
                    {"path": "app.py", "status": "M"},
                ],
                diff=diff,
            ),
        )
        self.assertNotIn("SECRET_KEY", inp.prompt)
        self.assertIn("safe = True", inp.prompt)

    def test_diff_content_starting_with_header_marker_is_not_split(self) -> None:
        diff = (
            "--- app.py\n"
            "+++ app.py\n"
            "@@ -1 +1 @@\n"
            "--- removed header\n"
            "+safe = True\n"
        )
        inp = prepare_review_input(
            project="E:/demo",
            task="fix",
            writer_summary="done",
            changes=_changes([{"path": "app.py", "status": "M"}], diff=diff),
        )
        self.assertIn("removed header", inp.prompt)
        self.assertIn("safe = True", inp.prompt)

    def test_unparseable_diff_block_marks_scope_incomplete(self) -> None:
        diff = (
            "diff --git malformed\n"
            "@@ -1 +1 @@\n"
            "-old\n"
            "+new\n"
        )
        inp = prepare_review_input(
            project="E:/demo",
            task="fix",
            writer_summary="done",
            changes=_changes([{"path": "app.py", "status": "M"}], diff=diff),
        )
        self.assertTrue(inp.scope.diff_truncated)
        self.assertFalse(inp.scope.is_complete)

    def test_rename_from_sensitive_path_excludes_old_file_content(self) -> None:
        diff = (
            "diff --git a/.env b/app.py\n"
            "similarity index 80%\n"
            "rename from .env\n"
            "rename to app.py\n"
            "--- a/.env\n"
            "+++ b/app.py\n"
            "@@ -1 +1 @@\n"
            "-SECRET_KEY=sk-live-abcdefghij1234567890XYZ\n"
            "+safe = True\n"
        )
        inp = prepare_review_input(
            project="E:/demo",
            task="fix",
            writer_summary="done",
            changes=_changes(
                [{"path": "app.py", "previous_path": ".env", "status": "R"}],
                diff=diff,
            ),
        )
        self.assertNotIn("SECRET_KEY", inp.prompt)
        self.assertNotIn("sk-live-abcdefghij1234567890XYZ", inp.prompt)
        self.assertIn("app.py", inp.scope.excluded_files)


class SensitiveTests(unittest.TestCase):
    def test_sensitive_file_content_is_not_sent(self) -> None:
        inp = prepare_review_input(
            project="E:/demo",
            task="fix",
            writer_summary="done",
            changes=_changes(
                [{"path": ".env", "status": "M"}],
                diff='+SECRET_KEY=abc123\n',
            ),
        )
        self.assertNotIn("SECRET_KEY", inp.prompt)
        self.assertIn(".env", list(inp.scope.excluded_files))

    def test_secret_value_in_diff_or_log_is_not_sent(self) -> None:
        secret = "sk-live-abcdefghij1234567890XYZ"
        inp = prepare_review_input(
            project="E:/demo",
            task="fix",
            writer_summary="done",
            changes=_changes(
                [{"path": "app.py", "status": "M"}],
                diff=f'+key="{secret}"\n',
            ),
            recent_log=f"ran with {secret}",
        )
        self.assertNotIn(secret, inp.prompt)

    def test_redaction_preserves_original_anchor_coordinates(self) -> None:
        diff = (
            "diff --git a/app.py b/app.py\n"
            "--- a/app.py\n"
            "+++ b/app.py\n"
            "@@ -10,4 +10,4 @@\n"
            '-old "sk-live-abcdefghij1234567890XYZ"\n'
            '+new "sk-live-abcdefghij1234567890XYZ"\n'
        )
        inp = prepare_review_input(
            project="E:/demo",
            task="fix",
            writer_summary="done",
            changes=_changes([{"path": "app.py", "status": "M"}], diff=diff),
        )
        # hunk header survives in both summary and diff; secret value is gone
        self.assertIn("@@ -10,4 +10,4 @@", inp.prompt)
        self.assertNotIn("sk-live-abcdefghij1234567890XYZ", inp.prompt)

    def test_ordinary_code_identifiers_are_not_mass_filtered(self) -> None:
        diff = (
            "diff --git a/app.py b/app.py\n"
            "+token = get_token(user)\n"
            "+password_field = 'label'\n"
        )
        inp = prepare_review_input(
            project="E:/demo",
            task="fix",
            writer_summary="done",
            changes=_changes([{"path": "app.py", "status": "M"}], diff=diff),
        )
        self.assertIn("token = get_token", inp.prompt)

    def test_review_projection_does_not_mutate_original_changes(self) -> None:
        original = _changes(
            [{"path": "app.py", "status": "M"}],
            diff='+key="sk-live-abcdefghij1234567890XYZ"\n',
        )
        before = dict(original)
        prepare_review_input(
            project="E:/demo",
            task="fix",
            writer_summary="done",
            changes=original,
        )
        self.assertEqual(original["diff"], before["diff"])

    def test_normal_safe_input_preserves_existing_prompt(self) -> None:
        changes = _changes(
            [{"path": "app.py", "status": "M", "additions": 1, "deletions": 1}],
            diff="diff --git a/app.py b/app.py\n-old\n+new\n",
        )
        inp = prepare_review_input(
            project="E:/demo",
            task="Fix the bug",
            writer_summary="done",
            changes=changes,
            recent_log="exit 0: python -m unittest",
        )
        expected = review.render_review_prompt(
            project="E:/demo",
            task="Fix the bug",
            writer_summary="done",
            changes=changes,
            recent_log="exit 0: python -m unittest",
        )
        self.assertEqual(inp.prompt, expected)
        self.assertTrue(inp.scope.is_complete)


if __name__ == "__main__":
    unittest.main()
