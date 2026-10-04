"""Git paths, finding location and dedupe limits (batch 2)."""
from __future__ import annotations

import unittest

from codey.reviews import core as review
from codey.workspace import changes as change_col


def _changes(files, diff=""):
    return {
        "ok": True,
        "changed_count": len(files),
        "files": files,
        "diff": diff,
    }


DIFF_APP = (
    "diff --git a/app.py b/app.py\n"
    "--- a/app.py\n"
    "+++ b/app.py\n"
    "@@ -10,4 +10,4 @@\n"
    "-old\n"
    "+new\n"
)


class GitStatusPathTests(unittest.TestCase):
    def test_git_status_preserves_space_and_unicode_paths(self) -> None:
        files = change_col.parse_git_status(" M my file.py\n M caf\u00e9.py\n")
        paths = [f["path"] for f in files]
        self.assertIn("my file.py", paths)
        self.assertIn("caf\u00e9.py", paths)

    def test_git_status_decodes_rename_source_and_target(self) -> None:
        files = change_col.parse_git_status("R  old.py -> new.py\n")
        self.assertEqual(files[0]["path"], "new.py")
        self.assertEqual(files[0].get("previous_path"), "old.py")

    def test_git_numstat_rename_matches_status_target(self) -> None:
        stats: dict[str, dict[str, int]] = {}
        change_col._merge_numstat(stats, "1\t0\told.py => new.py\n")
        self.assertEqual(stats, {"new.py": {"additions": 1, "deletions": 0}})

    def test_git_quoted_path_matches_diff_hunks(self) -> None:
        # Git C-style quoting with octal escapes must decode to the real path
        files = change_col.parse_git_status(' M "a b.py"\n')
        # quoted input from porcelain should not retain quotes
        self.assertNotIn('"a b.py"', [f["path"] for f in files])

    def test_literal_arrow_in_filename_is_not_assumed_rename(self) -> None:
        files = change_col.parse_git_status(" M a -> b.py\n")
        # status M with literal arrow in name must not split as rename
        self.assertEqual(files[0]["path"], "a -> b.py")
        self.assertNotIn("previous_path", files[0])

    def test_deleted_file_remains_reviewable(self) -> None:
        files = change_col.parse_git_status(" D gone.py\n")
        self.assertEqual(files[0]["path"], "gone.py")

    def test_unsafe_absolute_and_parent_paths_are_rejected(self) -> None:
        files = change_col.parse_git_status(" M /etc/passwd\n M ../other.py\n M C:/x.py\n")
        self.assertEqual(files, [])

    def test_incomplete_git_record_is_not_silently_accepted(self) -> None:
        files = change_col.parse_git_status("M\n")
        self.assertEqual(files, [])


class FindingLocationTests(unittest.TestCase):
    def test_normalized_finding_uses_canonical_changed_path(self) -> None:
        result = review.parse_review_response(
            '{"verdict":"changes_requested","summary":"x",'
            '"findings":[{"path":"app.py","issue":"bug"}]}',
            changes=_changes([{"path": "app.py", "status": "M"}], DIFF_APP),
        )
        self.assertEqual(result.findings[0].path, "app.py")

    def test_unknown_path_does_not_reach_writer(self) -> None:
        result = review.parse_review_response(
            '{"verdict":"changes_requested","summary":"x",'
            '"findings":[{"path":"invented.py","issue":"bug"}]}',
            changes=_changes([{"path": "app.py", "status": "M"}], DIFF_APP),
        )
        self.assertEqual(result.findings, [])
        self.assertEqual(result.verdict, "unknown")

    def test_missing_path_does_not_reach_writer(self) -> None:
        result = review.parse_review_response(
            '{"verdict":"changes_requested","summary":"x",'
            '"findings":[{"issue":"bug"}]}',
            changes=_changes([{"path": "app.py", "status": "M"}], DIFF_APP),
        )
        self.assertEqual(result.findings, [])

    def test_valid_path_with_invalid_anchor_becomes_path_only(self) -> None:
        result = review.parse_review_response(
            '{"verdict":"changes_requested","summary":"x",'
            '"findings":[{"path":"app.py","issue":"bug","hunk_index":99,"new_line":500}]}',
            changes=_changes([{"path": "app.py", "status": "M"}], DIFF_APP),
        )
        self.assertEqual(len(result.findings), 1)
        self.assertEqual(result.findings[0].path, "app.py")
        self.assertIsNone(result.findings[0].hunk_index)
        self.assertIsNone(result.findings[0].new_line)

    def test_boolean_and_nonpositive_line_numbers_are_rejected(self) -> None:
        result = review.parse_review_response(
            '{"verdict":"changes_requested","summary":"x","findings":['
            '{"path":"app.py","issue":"a","new_line":true},'
            '{"path":"app.py","issue":"b","new_line":0},'
            '{"path":"app.py","issue":"c","new_line":-3}]}',
            changes=_changes([{"path": "app.py", "status": "M"}], DIFF_APP),
        )
        for finding in result.findings:
            self.assertIsNone(finding.new_line)

    def test_rename_target_is_actionable(self) -> None:
        result = review.parse_review_response(
            '{"verdict":"changes_requested","summary":"x",'
            '"findings":[{"path":"new.py","issue":"bug"}]}',
            changes={
                "ok": True,
                "changed_count": 1,
                "files": [{"path": "new.py", "status": "R", "previous_path": "old.py"}],
                "diff": DIFF_APP.replace("a/app.py", "a/new.py").replace("b/app.py", "b/new.py"),
            },
        )
        self.assertEqual(len(result.findings), 1)

    def test_rename_source_not_in_current_paths_is_not_actionable(self) -> None:
        result = review.parse_review_response(
            '{"verdict":"changes_requested","summary":"x",'
            '"findings":[{"path":"old.py","issue":"bug"}]}',
            changes={
                "ok": True,
                "changed_count": 1,
                "files": [{"path": "new.py", "status": "R", "previous_path": "old.py"}],
                "diff": DIFF_APP.replace("a/app.py", "a/new.py").replace("b/app.py", "b/new.py"),
            },
        )
        self.assertEqual(result.findings, [])

    def test_deleted_file_old_line_anchor_is_supported(self) -> None:
        diff_deleted = (
            "diff --git a/gone.py b/gone.py\n"
            "deleted file mode 100644\n"
            "--- a/gone.py\n"
            "+++ /dev/null\n"
            "@@ -5,3 +0,0 @@\n"
            "-a\n-b\n-c\n"
        )
        result = review.parse_review_response(
            '{"verdict":"changes_requested","summary":"x",'
            '"findings":[{"path":"gone.py","issue":"bug","old_line":6}]}',
            changes=_changes([{"path": "gone.py", "status": "D"}], diff_deleted),
        )
        self.assertEqual(len(result.findings), 1)
        self.assertEqual(result.findings[0].old_line, 6)


class DedupeLimitTests(unittest.TestCase):
    def _many_changes(self):
        return _changes([{"path": "app.py", "status": "M"}], DIFF_APP)

    def test_exact_duplicates_are_removed(self) -> None:
        result = review.parse_review_response(
            '{"verdict":"changes_requested","summary":"x","findings":['
            '{"path":"app.py","issue":"same"},'
            '{"path":"app.py","issue":"same"}]}',
            changes=self._many_changes(),
        )
        self.assertEqual(len(result.findings), 1)

    def test_duplicates_do_not_consume_finding_limit(self) -> None:
        findings = ",".join(
            ['{"path":"app.py","issue":"same"}'] * 8
            + ['{"path":"app.py","issue":"unique-final"}']
        )
        result = review.parse_review_response(
            '{"verdict":"changes_requested","summary":"x","findings":[' + findings + "]}",
            changes=self._many_changes(),
        )
        issues = [f.issue for f in result.findings]
        self.assertIn("unique-final", issues)

    def test_invalid_entries_do_not_hide_later_valid_findings(self) -> None:
        result = review.parse_review_response(
            '{"verdict":"changes_requested","summary":"x","findings":['
            '{"path":"app.py"},'
            '{"path":"app.py","issue":{"x":1}},'
            '{"path":"app.py","issue":"good"}]}',
            changes=self._many_changes(),
        )
        self.assertEqual([f.issue for f in result.findings], ["good"])

    def test_different_anchor_preserves_distinct_findings(self) -> None:
        diff_two = (
            "diff --git a/app.py b/app.py\n"
            "--- a/app.py\n"
            "+++ b/app.py\n"
            "@@ -10,4 +10,4 @@\n"
            "-a\n+b\n"
            "@@ -20,4 +20,4 @@\n"
            "-c\n+d\n"
        )
        result = review.parse_review_response(
            '{"verdict":"changes_requested","summary":"x","findings":['
            '{"path":"app.py","issue":"same","new_line":11},'
            '{"path":"app.py","issue":"same","new_line":21}]}',
            changes=_changes([{"path": "app.py", "status": "M"}], diff_two),
        )
        self.assertEqual(len(result.findings), 2)

    def test_code_whitespace_difference_is_not_collapsed(self) -> None:
        result = review.parse_review_response(
            '{"verdict":"changes_requested","summary":"x","findings":['
            '{"path":"app.py","issue":"a  b"},'
            '{"path":"app.py","issue":"a b"}]}',
            changes=self._many_changes(),
        )
        self.assertEqual(len(result.findings), 2)

    def test_model_order_is_preserved(self) -> None:
        result = review.parse_review_response(
            '{"verdict":"changes_requested","summary":"x","findings":['
            '{"path":"app.py","issue":"first"},'
            '{"path":"app.py","issue":"second"}]}',
            changes=self._many_changes(),
        )
        self.assertEqual([f.issue for f in result.findings], ["first", "second"])

    def test_candidate_budget_exhaustion_marks_incomplete(self) -> None:
        many = ",".join([f'{{"path":"app.py","issue":"i{i}"}}' for i in range(80)])
        result = review.parse_review_response(
            '{"verdict":"changes_requested","summary":"x","findings":[' + many + "]}",
            changes=self._many_changes(),
        )
        self.assertEqual(result.status, "incomplete")
        self.assertFalse(result.is_complete)


if __name__ == "__main__":
    unittest.main()
