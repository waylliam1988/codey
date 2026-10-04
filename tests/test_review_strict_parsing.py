"""Strict review parsing and verdict semantics (batch 1)."""
from __future__ import annotations

import unittest

from codey.reviews import core as review

CHANGES = {
    "ok": True,
    "changed_count": 1,
    "files": [{"path": "app.py", "status": "M"}],
    "diff": (
        "diff --git a/app.py b/app.py\n"
        "--- a/app.py\n"
        "+++ b/app.py\n"
        "@@ -10,4 +10,4 @@\n"
        "-old\n"
        "+new\n"
    ),
}


class StrictParsingTests(unittest.TestCase):
    def test_empty_json_is_not_approved(self) -> None:
        with self.assertRaises(ValueError):
            review.parse_review_response("{}", changes=CHANGES)

    def test_metadata_json_before_review_is_not_selected(self) -> None:
        text = (
            '{"type":"task_done","run_id":"r1"} '
            '{"verdict":"changes_requested","summary":"Fix it",'
            '"findings":[{"path":"app.py","issue":"Bad edge"}]}'
        )
        result = review.parse_review_response(text, changes=CHANGES)
        self.assertEqual(result.verdict, "changes_requested")
        self.assertEqual(result.findings[0].path, "app.py")

    def test_unknown_verdict_is_not_approved(self) -> None:
        result = review.parse_review_response(
            '{"verdict":"maybe","summary":"unsure","findings":[]}',
            changes=CHANGES,
        )
        self.assertFalse(result.approved)
        self.assertEqual(result.verdict, "unknown")
        self.assertEqual(result.status, "incomplete")

    def test_missing_verdict_without_findings_is_invalid(self) -> None:
        with self.assertRaises(ValueError):
            review.parse_review_response(
                '{"summary":"looks fine"}', changes=CHANGES
            )

    def test_findings_wrong_type_is_invalid(self) -> None:
        with self.assertRaises(ValueError):
            review.parse_review_response(
                '{"verdict":"approved","summary":"ok","findings":{"path":"app.py"}}',
                changes=CHANGES,
            )

    def test_issue_object_is_not_coerced_to_string(self) -> None:
        result = review.parse_review_response(
            '{"verdict":"changes_requested","summary":"x",'
            '"findings":[{"path":"app.py","issue":{"text":"boom"}}]}',
            changes=CHANGES,
        )
        self.assertEqual(result.findings, [])

    def test_approved_with_actionable_findings_requests_changes(self) -> None:
        result = review.parse_review_response(
            '{"verdict":"approved","summary":"ok",'
            '"findings":[{"path":"app.py","issue":"Real bug"}]}',
            changes=CHANGES,
        )
        self.assertEqual(result.verdict, "changes_requested")
        self.assertFalse(result.approved)
        self.assertTrue(result.needs_writer_repair)

    def test_incomplete_without_findings_does_not_request_writer_repair(self) -> None:
        result = review.ReviewResult(
            "changes_requested",
            "partial",
            [],
            status="incomplete",
        )
        self.assertFalse(result.needs_writer_repair)

    def test_conflicting_review_objects_are_rejected(self) -> None:
        text = (
            '{"verdict":"approved","summary":"good","findings":[]} '
            '{"verdict":"changes_requested","summary":"bad",'
            '"findings":[{"path":"app.py","issue":"Bug"}]}'
        )
        with self.assertRaises(ValueError):
            review.parse_review_response(text, changes=CHANGES)

    def test_valid_review_with_light_prose_is_supported(self) -> None:
        result = review.parse_review_response(
            'Here is my review:\n```json\n'
            '{"verdict":"approved","summary":"Looks good","findings":[]}\n'
            '```\nThanks.',
            changes=CHANGES,
        )
        self.assertTrue(result.approved)

    def test_invalid_reply_uses_existing_single_format_repair(self) -> None:
        calls: list[str] = []

        def send_repair(prompt: str) -> str:
            calls.append(prompt)
            return '{"verdict":"approved","summary":"Looks good","findings":[]}'

        result = review.parse_review_with_repair("not json at all", send_repair, changes=CHANGES)
        self.assertTrue(result.approved)
        self.assertEqual(len(calls), 1)

    def test_unknown_verdict_uses_single_format_repair(self) -> None:
        calls: list[str] = []

        def send_repair(prompt: str) -> str:
            calls.append(prompt)
            return '{"verdict":"approved","summary":"Looks good","findings":[]}'

        result = review.parse_review_with_repair(
            '{"verdict":"maybe","summary":"unsure","findings":[]}',
            send_repair,
            changes=CHANGES,
        )
        self.assertTrue(result.approved)
        self.assertEqual(len(calls), 1)

    def test_second_invalid_reply_never_becomes_approved(self) -> None:
        def send_repair(_prompt: str) -> str:
            return "still not json"

        with self.assertRaises(ValueError):
            review.parse_review_with_repair("not json", send_repair, changes=CHANGES)

    def test_oversized_reply_never_becomes_approved(self) -> None:
        big = "x" * (review.MAX_REVIEW_REPLY_CHARS + 10)
        with self.assertRaises(ValueError):
            review.parse_review_response(big, changes=CHANGES)

    def test_duplicate_contract_keys_are_rejected(self) -> None:
        with self.assertRaises(ValueError):
            review.parse_review_response(
                '{"verdict":"changes_requested","verdict":"approved","findings":[]}',
                changes=CHANGES,
            )


if __name__ == "__main__":
    unittest.main()
