"""Review contract integration: faults, cold start and final gate (batch 8)."""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest import mock

from codey.reviews.coordinator import ReviewCoordinator
from codey.reviews.core import ReviewResult, parse_review_response
from codey.runtime.core.run_result import RunResult

CHANGES = {
    "ok": True,
    "changed_count": 1,
    "files": [{"path": "app.py", "status": "M"}],
    "diff": "diff --git a/app.py b/app.py\n-old\n+new\n",
}


def _coordinator(run_review):
    return ReviewCoordinator(mock.Mock(return_value=CHANGES)).run_cycle(
        project="project",
        tracker=object(),
        session_id="s",
        task="t",
        result=RunResult("done", "done", 1, False, True),
        task_changed=True,
        changes=CHANGES,
        changes_dirty=False,
        writer_id="w",
        recent_log="log",
        render_change_brief=mock.Mock(return_value=""),
        execution_evidence="",
        successful_checks=(),
        checkpoint_prompt="cp",
        checks_before_review_followup=False,
        stop_requested=mock.Mock(return_value=False),
        refresh_project_map=mock.Mock(return_value=""),
        build_verification_map=mock.Mock(return_value=""),
        run_review=run_review,
        close_writer_for_review=mock.Mock(),
        repair_writer=mock.Mock(return_value=RunResult("fixed", "done", 1, False, True)),
        set_checkpoint_status=mock.Mock(),
        emit_review_unavailable=mock.Mock(),
    )


class ContractIntegrationTests(unittest.TestCase):
    def test_input_collection_failure_never_yields_complete_approved(self) -> None:
        bad = {"ok": False, "error": "git failed", "files": [], "diff": ""}
        with self.assertRaises(ValueError):
            parse_review_response("{}", changes=bad)

    def test_reply_format_repair_failure_never_defaults_approved(self) -> None:
        from codey.reviews.core import parse_review_with_repair

        with self.assertRaises(ValueError):
            parse_review_with_repair("bad", lambda _: "still bad", changes=CHANGES)

    def test_reply_after_file_change_is_stale_not_repaired(self) -> None:
        review = ReviewResult("changes_requested", "x", [], status="stale")
        # stale with no findings must not repair; Coordinator returns without repair
        result = _coordinator(mock.Mock(return_value=("r", review)))
        self.assertFalse(result.review_repair_attempted)

    def test_artifact_without_ledger_finished_is_not_reusable(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            from codey.reviews.core import ReviewFinding
            from codey.reviews.input import ReviewScope
            from codey.reviews.persistence import ReviewArtifactStore, save_review_artifact

            store = ReviewArtifactStore(Path(td))
            scope = ReviewScope(total_changed_files=1, provided_files=("app.py",))
            result = ReviewResult(
                "changes_requested", "x", [ReviewFinding("app.py", "bug")],
                status="complete", scope=scope,
            )
            ref = save_review_artifact(
                store, session_id="s", run_id="r1", attempt_id="a1",
                result=result, scope_digest="s", prompt_digest="p",
                snapshot_digest="snap", reviewer_id="r", policy="web_if_available",
            )
            self.assertIsNotNone(ref)
            # no ledger finished event -> try_reuse must miss (no ledger)
            from codey.reviews.identity import ReviewIdentity
            from codey.reviews.reuse import try_reuse_review

            identity = ReviewIdentity(
                scope_digest="s", prompt_digest="p", snapshot_digest="snap",
                project="proj", reviewer_id="r", model_id="m1",
                contract_version=1, policy="web_if_available", self_review=False,
            )
            reused = try_reuse_review(
                state_home=td, session_id="s", current_run_id="r2",
                current_project="proj", source_run_id="r1",
                current_scope=scope, current_identity=identity,
                current_snapshot_ok=True,
            )
            self.assertIsNone(reused)

    def test_corrupt_ledger_does_not_reuse_readable_prefix(self) -> None:
        from codey.runs.ledger import read_ledger

        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "ledger.jsonl"
            path.write_text('{"schema_version":1,"seq":1,"type":"run_started"}\nbad json\n', encoding="utf-8")
            self.assertEqual(read_ledger(path), [])

    def test_repeated_terminal_events_do_not_duplicate_repair(self) -> None:
        from codey.reviews.core import ReviewFinding

        review = ReviewResult(
            "changes_requested", "x", [ReviewFinding("app.py", "bug")], status="complete"
        )
        repair = mock.Mock(return_value=RunResult("fixed", "done", 1, False, True))
        coord = ReviewCoordinator(mock.Mock(return_value=CHANGES))
        first = coord.run_cycle(
            project="p", tracker=object(), session_id="s", task="t",
            result=RunResult("done", "done", 1, False, True),
            task_changed=True, changes=CHANGES, changes_dirty=False,
            writer_id="w", recent_log="", render_change_brief=mock.Mock(return_value=""),
            execution_evidence="", successful_checks=(),
            checkpoint_prompt="cp", checks_before_review_followup=False,
            stop_requested=mock.Mock(return_value=False),
            refresh_project_map=mock.Mock(return_value=""),
            build_verification_map=mock.Mock(return_value=""),
            run_review=mock.Mock(return_value=("r", review)),
            close_writer_for_review=mock.Mock(), repair_writer=repair,
            set_checkpoint_status=mock.Mock(), emit_review_unavailable=mock.Mock(),
        )
        self.assertTrue(first.review_repair_attempted)
        self.assertEqual(repair.call_count, 1)

    def test_output_schema_drop_is_contract_failure(self) -> None:
        from codey.app import event_payloads as _payloads

        event = {"type": "review", "run_id": "r", "session_id": "s", "text": "hi"}
        payload = _payloads.machine_event_payload(event)
        self.assertIsNotNone(payload)
        self.assertEqual(payload["type"], "review")


if __name__ == "__main__":
    unittest.main()
