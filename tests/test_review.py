from __future__ import annotations

import unittest

from codey import review


class ReviewProtocolTests(unittest.TestCase):
    def test_parse_approved_review_json(self) -> None:
        result = review.parse_review_response(
            '{"verdict":"approved","summary":"Looks good","findings":[]}'
        )

        self.assertTrue(result.approved)
        self.assertEqual(result.summary, "Looks good")
        self.assertEqual(result.findings, [])

    def test_parse_review_with_light_prose_and_findings(self) -> None:
        result = review.parse_review_response(
            'Here is the review:\n'
            '{"verdict":"changes_requested","summary":"Fix one edge case",'
            '"findings":[{"path":"app.py","issue":"Empty input fails",'
            '"suggested_fix":"Add a guard"}]}'
        )

        self.assertFalse(result.approved)
        self.assertEqual(result.findings[0].path, "app.py")
        self.assertEqual(result.findings[0].issue, "Empty input fails")
        self.assertEqual(result.findings[0].suggested_fix, "Add a guard")

    def test_findings_imply_changes_requested_when_verdict_missing(self) -> None:
        result = review.parse_review_response(
            '{"summary":"Needs work","findings":[{"issue":"Test is missing"}]}'
        )

        self.assertEqual(result.verdict, "changes_requested")

    def test_render_review_prompt_is_read_only_and_json_only(self) -> None:
        prompt = review.render_review_prompt(
            project="E:/demo",
            task="Fix the bug",
            writer_summary="done",
            changes={
                "files": [{"path": "app.py", "status": "M", "additions": 1, "deletions": 1}],
                "diff": "diff --git a/app.py b/app.py\n-old\n+new\n",
            },
            recent_log="exit 0: python -m unittest",
        )

        self.assertIn("read-only", prompt)
        self.assertIn("You are a careful code reviewer", prompt)
        self.assertIn("Every findings[].path must be copied from the Changed files list", prompt)
        self.assertIn("Do not invent filenames", prompt)
        self.assertIn("If the issue is a missing test", prompt)
        self.assertIn("Return only JSON. No analysis. No explanation.", prompt)
        self.assertIn("Return exactly one JSON object", prompt)
        self.assertIn("M app.py +1 -1", prompt)
        self.assertIn("<copy path from Changed files>", prompt)
        self.assertNotIn("relative/file.py", prompt)
        self.assertIn("exit 0: python -m unittest", prompt)
        self.assertNotIn("Codey's second model", prompt)

    def test_render_review_prompt_warns_when_diff_is_truncated(self) -> None:
        prompt = review.render_review_prompt(
            project="E:/demo",
            task="Fix the bug",
            writer_summary="done",
            changes={
                "files": [{"path": "app.py", "status": "M", "additions": 1, "deletions": 1}],
                "diff": "diff --git a/app.py b/app.py\n-old\n+new\n",
                "truncated": True,
            },
        )

        self.assertIn("Diff truncation note", prompt)
        self.assertIn("avoid assuming omitted hunks are clean", prompt)

    def test_render_review_prompt_includes_change_brief_for_intent_review(self) -> None:
        prompt = review.render_review_prompt(
            project="E:/demo",
            task="Build a snake game",
            writer_summary="done",
            changes={
                "files": [{"path": "app.py", "status": "A", "additions": 10, "deletions": 0}],
                "diff": "diff --git a/app.py b/app.py\n+print('snake')\n",
            },
            change_brief=(
                "Private ChangeBrief:\n"
                "- User intent: runnable snake game\n"
                "Acceptance checks:\n"
                "- run python app.py"
            ),
        )

        self.assertIn("Private ChangeBrief", prompt)
        self.assertEqual(prompt.count("Private ChangeBrief"), 1)
        self.assertIn("runnable snake game", prompt)
        self.assertIn("intent is satisfied", prompt)
        self.assertIn("acceptance checks are covered", prompt)
        self.assertIn("non-goals were not violated", prompt)
        self.assertIn("risks were addressed or explicitly deferred", prompt)

    def test_render_review_prompt_includes_project_map_without_relaxing_path_rule(self) -> None:
        prompt = review.render_review_prompt(
            project="E:/demo",
            task="Update auth flow",
            writer_summary="done",
            changes={
                "files": [{"path": "src/session.py", "status": "M", "additions": 3, "deletions": 1}],
                "diff": "diff --git a/src/session.py b/src/session.py\n+TOKEN_TTL = 60\n",
            },
            project_map=(
                "Project Map (bounded local scan; relative paths only):\n"
                "Source/test roots:\n- src/\n- tests/\n"
                "Key files:\n- tests/test_session.py"
            ),
        )

        self.assertIn("Project Map", prompt)
        self.assertIn("tests/test_session.py", prompt)
        self.assertIn("never use a path from the Project Map as findings[].path", prompt)
        self.assertIn("Every findings[].path must be copied from the Changed files list", prompt)
        self.assertIn("M src/session.py +3 -1", prompt)

    def test_render_review_prompt_treats_verification_map_as_candidates_only(self) -> None:
        prompt = review.render_review_prompt(
            project="E:/demo",
            task="Fix auth",
            writer_summary="done",
            changes={
                "files": [{"path": "src/auth.py", "status": "M"}],
                "diff": "diff --git a/src/auth.py b/src/auth.py\n+fixed\n",
            },
            verification_map=(
                "Verification Map (bounded candidates; not coverage proof):\n"
                "Existing test candidates found locally (not necessarily changed):\n"
                "- tests/test_auth.py: directly imports changed module [evidence: import]"
            ),
        )

        self.assertIn("Verification Map (bounded candidates", prompt)
        self.assertIn("not proof of impact or coverage", prompt)
        self.assertIn("Do not request a test merely because", prompt)
        self.assertIn("existing readable local file", prompt)
        self.assertIn("not modified, not that it is missing", prompt)
        self.assertIn("do not relax the Changed-files-only", prompt)

    def test_render_review_prompt_includes_bounded_execution_evidence(self) -> None:
        prompt = review.render_review_prompt(
            project="E:/demo",
            task="Fix auth",
            writer_summary="done",
            changes={
                "files": [{"path": "src/auth.py", "status": "M"}],
                "diff": "diff --git a/src/auth.py b/src/auth.py\n+fixed\n",
            },
            execution_evidence=(
                "Execution Evidence (bounded local facts):\n"
                "- Latest edit epoch: 1\n"
                "- Successful checks after latest edit: python -m pytest"
            ),
        )

        self.assertIn("Execution Evidence (bounded local facts)", prompt)
        self.assertIn("Successful checks after latest edit", prompt)

    def test_render_writer_followup_keeps_reviewer_advisory(self) -> None:
        result = review.ReviewResult(
            verdict="changes_requested",
            summary="Fix one issue",
            findings=[review.ReviewFinding("app.py", "Missing test", "Add a unittest")],
        )

        prompt = review.render_writer_followup("Original task", result)

        self.assertIn("Treat the review as advisory", prompt)
        self.assertIn("Continue the task in this same project.", prompt)
        self.assertIn("Reviewer paths are only clues", prompt)
        self.assertIn("If a referenced path does not exist", prompt)
        self.assertIn("do not invent a change", prompt)
        self.assertIn("Original task", prompt)
        self.assertIn("app.py", prompt)
        self.assertIn("Missing test", prompt)
        self.assertNotIn("Continue the Codey task", prompt)

    def test_render_writer_followup_keeps_change_brief_when_present(self) -> None:
        result = review.ReviewResult(
            verdict="changes_requested",
            summary="Fix scope issue",
            findings=[review.ReviewFinding("app.py", "Intent not satisfied")],
        )

        prompt = review.render_writer_followup(
            "Original task",
            result,
            change_brief="Private ChangeBrief:\n- User intent: keep CLI behavior",
        )

        self.assertIn("Private ChangeBrief", prompt)
        self.assertEqual(prompt.count("Private ChangeBrief"), 1)
        self.assertIn("keep CLI behavior", prompt)

    def test_review_repair_prompt_is_json_only(self) -> None:
        prompt = review.review_repair_prompt()

        self.assertIn("Return only the JSON object now", prompt)
        self.assertIn("preserving your previous verdict", prompt)
        self.assertIn("must still be copied from the Changed files list", prompt)
        self.assertIn("<copy path from Changed files>", prompt)
        self.assertNotIn("relative/file.py", prompt)
        self.assertIn('"verdict":"approved"', prompt)
        self.assertIn('"verdict":"changes_requested"', prompt)
        self.assertNotIn("Codey", prompt)

    def test_parse_review_with_repair_uses_one_json_repair_turn(self) -> None:
        sent: list[str] = []

        def send_repair(prompt: str) -> str:
            sent.append(prompt)
            return '{"verdict":"approved","summary":"Looks good","findings":[]}'

        result = review.parse_review_with_repair("not json", send_repair)

        self.assertTrue(result.approved)
        self.assertEqual(len(sent), 1)
        self.assertIn("Return only the JSON object now", sent[0])

    def test_reviewable_changes_require_diff(self) -> None:
        self.assertFalse(review.has_reviewable_changes({"ok": True, "changed_count": 1, "diff": ""}))
        self.assertTrue(review.has_reviewable_changes({"ok": True, "changed_count": 1, "diff": "+x"}))


if __name__ == "__main__":
    unittest.main()
