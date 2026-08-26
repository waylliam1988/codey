from __future__ import annotations

import unittest

from codey.events import RunEvent
from codey.execution_evidence import ExecutionEvidence, check_failure_summary
from codey.models import ToolCall
from codey.tool_runtime import ToolOutcome
from codey.work_checkpoint import CheckpointCheck


def event(name: str, args: dict, outcome: ToolOutcome, turn: int = 1) -> RunEvent:
    return RunEvent.tool_finished(turn, ToolCall(name, args), outcome)


class ExecutionEvidenceTests(unittest.TestCase):
    def test_records_bounded_reads_searches_and_duplicate_information(self) -> None:
        evidence = ExecutionEvidence()
        read = event(
            "read",
            {"path": "src/auth.py", "offset": 20, "limit": 40},
            ToolOutcome("page", True, truncated=True),
        )
        search = event(
            "search",
            {"path": ".", "query": "validate_token"},
            ToolOutcome("matches", True),
        )

        evidence.record(read)
        evidence.record(read)
        evidence.record(search)

        self.assertEqual(evidence.duplicate_info_tools, 1)
        self.assertEqual(len(evidence.reads), 2)
        self.assertEqual(len(evidence.searches), 1)
        self.assertTrue(evidence.searches[0].complete)
        self.assertEqual(evidence.truncated_results[0].tool, "read")
        rendered = evidence.render_for_review()
        self.assertIn("src/auth.py", rendered)
        self.assertIn("search validate_token under .", rendered)
        self.assertIn("Repeated identical information calls within edit epochs: 1", rendered)
        self.assertIn("Truncated tool results during task: read src/auth.py:20", rendered)

    def test_edit_advances_epoch_and_invalidates_green_checks(self) -> None:
        evidence = ExecutionEvidence()
        evidence.seed_checks((CheckpointCheck("python -m pytest", "."),))
        evidence.record(event(
            "edit",
            {"path": "src/auth.py"},
            ToolOutcome("edited", True, changed=True),
        ))

        self.assertEqual(evidence.edit_epoch, 1)
        self.assertEqual(evidence.changed_files, ["src/auth.py"])
        self.assertEqual(evidence.successful_checks, ())

    def test_failed_run_clears_green_checks_and_success_resolves_same_failure(self) -> None:
        evidence = ExecutionEvidence()
        args = {"path": ".", "command": "python -m pytest"}
        evidence.record(event("run", args, ToolOutcome("ok", True, exit_code=0)))
        evidence.record(event("run", args, ToolOutcome("failed", False, exit_code=1)))

        self.assertFalse(evidence.has_successful_checks)
        self.assertEqual(len(evidence.failed_checks_after_edit), 1)

        evidence.record(event("run", args, ToolOutcome("ok", True, exit_code=0)))

        self.assertTrue(evidence.has_successful_checks)
        self.assertEqual(evidence.failed_checks_after_edit, [])

    def test_workspace_drift_invalidates_all_check_evidence(self) -> None:
        evidence = ExecutionEvidence()
        evidence.seed_checks((CheckpointCheck("python -m pytest", "."),))
        evidence.failed_checks_after_edit.append(
            evidence.successful_checks[0]
        )

        evidence.invalidate_checks()

        self.assertFalse(evidence.has_successful_checks)
        self.assertEqual(evidence.failed_checks_after_edit, [])

    def test_failure_summary_screens_markers_shapes_and_entropy(self) -> None:
        class Outcome:
            model_text = (
                "FAILED tests/test_auth.py\n"
                "Aa1Bb2Cc3Dd4Ee5Ff6Gg7Hh8Ii9Jj0Kk29\n"
                "api_key=sk-abcdefghijklmnop1234\n"
                "1 failed"
            )

        summary = check_failure_summary(Outcome())

        self.assertNotIn("Aa1Bb2", summary)
        self.assertNotIn("sk-abcdefghij", summary)
        self.assertIn("FAILED tests/test_auth.py", summary)
        self.assertIn("1 failed", summary)

    def test_truncated_search_is_not_reported_complete(self) -> None:
        evidence = ExecutionEvidence()
        evidence.record(event(
            "references",
            {"path": ".", "symbol": "SessionStore"},
            ToolOutcome("partial", True, truncated=True),
        ))

        rendered = evidence.render_for_review()
        self.assertIn("Complete searches after latest edit: (none observed)", rendered)
        self.assertIn("references SessionStore under .", rendered)


if __name__ == "__main__":
    unittest.main()
