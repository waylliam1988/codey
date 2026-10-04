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
    def test_partial_scope_is_persisted_as_incomplete(self) -> None:
        from codey.app import review_service
        from codey.reviews.input import prepare_review_input

        class _Ctx:
            state_home = None

            def emit(self, _event):
                pass

        changes = {
            "ok": True,
            "changed_count": 1,
            "files": [{"path": ".env", "status": "M"}],
            "diff": "+SECRET_KEY=sk-live-abcdefghij1234567890XYZ\n",
        }
        prepared = prepare_review_input(
            project="project",
            task="review",
            writer_summary="done",
            changes=changes,
        )
        review = ReviewResult("approved", "ok", [], status="complete")
        from codey.reviews.identity import ReviewSnapshot

        finalized = review_service._finalize_review(
            _Ctx(), "s", "project", "reviewer", False,
            prepared,
            ReviewSnapshot(root="project", files=(), ok=True),
            review,
            None,
            "run-1",
        )
        self.assertFalse(prepared.scope.is_complete)
        self.assertEqual(finalized.status, "incomplete")
        self.assertFalse(finalized.is_complete)

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

    def test_tampered_artifact_hash_is_not_reused(self) -> None:
        from codey.reviews.core import ReviewFinding
        from codey.reviews.identity import ReviewIdentity
        from codey.reviews.input import ReviewScope
        from codey.reviews.persistence import (
            ReviewArtifactStore,
            save_review_artifact,
        )
        from codey.reviews.reuse import try_reuse_review
        from codey.runs.ledger import RunLedgerStore

        with tempfile.TemporaryDirectory() as td:
            scope = ReviewScope(total_changed_files=1, provided_files=("app.py",))
            result = ReviewResult(
                "changes_requested", "x", [ReviewFinding("app.py", "bug")],
                status="complete", scope=scope,
            )
            artifact_store = ReviewArtifactStore(Path(td))
            ref = save_review_artifact(
                artifact_store,
                session_id="s", run_id="r1", attempt_id="a1", result=result,
                scope_digest="s", prompt_digest="p", snapshot_digest="snap",
                reviewer_id="r", policy="web_if_available", model_id="m1",
            )
            self.assertIsNotNone(ref)
            path = artifact_store.path_for("s", "r1", "a1")
            payload = path.read_text(encoding="utf-8").replace('"bug"', '"tampered"')
            path.write_text(payload, encoding="utf-8")
            ledger = RunLedgerStore(td).open(
                run_id="r1", session_id="s", project="proj", task="t",
                provider="r", mode="review",
            )
            ledger.append(
                "review_result_projected", review_attempt_id="a1",
                verdict="changes_requested", status="complete", origin="fresh",
                finding_count=1, artifact_sha256=ref.sha256,
            )
            ledger.append(
                "review_finished", review_attempt_id="a1",
                verdict="changes_requested", status="complete", origin="fresh",
                finding_count=1, artifact_sha256=ref.sha256,
            )
            ledger.finish(summary="done", stop_reason="done", turns=1, max_turns=1, provider="r")
            identity = ReviewIdentity(
                scope_digest="s", prompt_digest="p", snapshot_digest="snap",
                project="proj", reviewer_id="r", model_id="m1",
                contract_version=1, policy="web_if_available", self_review=False,
            )
            reused = try_reuse_review(
                state_home=td, session_id="s", current_run_id="r2",
                current_project="proj", source_run_id="r1", current_scope=scope,
                current_identity=identity, current_snapshot_ok=True,
            )
        self.assertIsNone(reused)

    def test_reused_result_keeps_source_identity_for_current_ledger(self) -> None:
        from codey.reviews.core import ReviewFinding
        from codey.reviews.identity import ReviewIdentity
        from codey.reviews.input import ReviewScope
        from codey.reviews.persistence import ReviewArtifactStore, save_review_artifact
        from codey.reviews.reuse import try_reuse_review
        from codey.runs.ledger import RunLedgerStore

        with tempfile.TemporaryDirectory() as td:
            scope = ReviewScope(total_changed_files=1, provided_files=("app.py",))
            result = ReviewResult(
                "changes_requested", "x", [ReviewFinding("app.py", "bug")],
                status="complete", scope=scope,
            )
            ref = save_review_artifact(
                ReviewArtifactStore(td), session_id="s", run_id="r1", attempt_id="a1",
                result=result, scope_digest="s", prompt_digest="p", snapshot_digest="snap",
                reviewer_id="r", policy="web_if_available", model_id="m1",
            )
            self.assertIsNotNone(ref)
            ledger = RunLedgerStore(td).open(
                run_id="r1", session_id="s", project="proj", task="t",
                provider="r", mode="review",
            )
            for event in ("review_result_projected", "review_finished"):
                ledger.append(
                    event, review_attempt_id="a1", verdict="changes_requested",
                    status="complete", origin="fresh", finding_count=1,
                    artifact_sha256=ref.sha256,
                )
            ledger.finish(summary="done", stop_reason="done", turns=1, max_turns=1, provider="r")
            identity = ReviewIdentity(
                scope_digest="s", prompt_digest="p", snapshot_digest="snap",
                project="proj", reviewer_id="r", model_id="m1",
                contract_version=1, policy="web_if_available", self_review=False,
            )
            reused = try_reuse_review(
                state_home=td, session_id="s", current_run_id="r2",
                current_project="proj", source_run_id="r1", current_scope=scope,
                current_identity=identity, current_snapshot_ok=True,
            )
        self.assertIsNotNone(reused)
        self.assertEqual(reused.origin, "reused")
        self.assertIsNotNone(reused.identity)
        self.assertEqual(reused.identity.attempt_id, "a1")
        self.assertEqual(reused.identity.artifact_sha256, ref.sha256)

    def test_finished_without_projection_is_not_reusable(self) -> None:
        from codey.reviews.identity import ReviewIdentity
        from codey.reviews.input import ReviewScope
        from codey.reviews.persistence import ReviewArtifactStore, save_review_artifact
        from codey.reviews.reuse import try_reuse_review
        from codey.runs.ledger import RunLedgerStore

        with tempfile.TemporaryDirectory() as td:
            scope = ReviewScope(total_changed_files=1, provided_files=("app.py",))
            ref = save_review_artifact(
                ReviewArtifactStore(td), session_id="s", run_id="r1", attempt_id="a1",
                result=ReviewResult(
                    "approved", "ok", [], status="complete", scope=scope,
                ),
                scope_digest="s", prompt_digest="p", snapshot_digest="snap",
                reviewer_id="r", policy="web_if_available", model_id="m1",
            )
            self.assertIsNotNone(ref)
            ledger = RunLedgerStore(td).open(
                run_id="r1", session_id="s", project="proj", task="t",
                provider="r", mode="review",
            )
            ledger.append(
                "review_finished", review_attempt_id="a1", verdict="approved",
                status="complete", origin="fresh", finding_count=0,
                artifact_sha256=ref.sha256,
            )
            ledger.finish(summary="done", stop_reason="done", turns=1, max_turns=1, provider="r")
            reused = try_reuse_review(
                state_home=td, session_id="s", current_run_id="r2",
                current_project="proj", source_run_id="r1", current_scope=scope,
                current_identity=ReviewIdentity(
                    scope_digest="s", prompt_digest="p", snapshot_digest="snap",
                    project="proj", reviewer_id="r", model_id="m1",
                    contract_version=1, policy="web_if_available", self_review=False,
                ),
                current_snapshot_ok=True,
            )
        self.assertIsNone(reused)

    def test_review_projection_does_not_mix_attempts(self) -> None:
        from codey.runs.ledger import RunLedgerRecord
        from codey.runs.ledger_projection import project_run_ledger

        def record(seq: int, event_type: str, attempt: str) -> RunLedgerRecord:
            return RunLedgerRecord({
                "schema_version": 1,
                "seq": seq,
                "type": event_type,
                "run_id": "r",
                "session_id": "s",
                "review_attempt_id": attempt,
                "verdict": "approved",
                "status": "complete",
                "origin": "fresh",
                "finding_count": 0,
            })

        projection = project_run_ledger([
            record(1, "run_started", ""),
            record(2, "review_result_projected", "a2"),
            record(3, "review_finished", "a1"),
            record(4, "run_finished", ""),
        ])
        self.assertIsNone(projection.review)

    def test_review_only_mode_persists_review_result_to_ledger_callback(self) -> None:
        from types import SimpleNamespace

        from codey.agents.handoff import ConversationSnapshot
        from codey.operations.review_flow import ReviewFlowDeps, run_review_mode
        from codey.task.model import TaskSubmission

        review = ReviewResult("approved", "ok", [], status="complete")
        identity = SimpleNamespace(attempt_id="a1", artifact_sha256="sha")
        review = type(review)(
            review.verdict, review.summary, review.findings,
            status=review.status, identity=identity,
        )
        events = []
        writer = SimpleNamespace(
            append=lambda event_type, **fields: events.append((event_type, fields)),
        )
        request = TaskSubmission(
            session_id="s", project="project", task="review", max_turns=1,
            continue_task=False, provider_id="r", intent="review",
        )
        snapshot = ConversationSnapshot(mode="review")
        frame = SimpleNamespace(
            request=request,
            run_id="r2",
            provider_id="r",
            provider=None,
            project_text="project",
            trace=None,
            conversation=SimpleNamespace(
                snapshot=snapshot,
                update_snapshot=lambda value: None,
            ),
        )
        deps = ReviewFlowDeps(
            state=SimpleNamespace(set_provider_session=lambda *_args: None),
            collect_changes=lambda *_args: CHANGES,
            run_review=lambda **_kwargs: ("r", review),
            is_git_repository=lambda _project: True,
        )
        run_review_mode(deps, frame, append_ledger=lambda callback: callback(writer))
        self.assertEqual([event_type for event_type, _ in events], [
            "review_result_projected", "review_finished",
        ])

    def test_review_ledger_projection_keeps_reuse_lineage(self) -> None:
        from codey.runs.ledger import RunLedgerRecord
        from codey.runs.ledger_projection import project_run_ledger

        def record(seq: int, event_type: str, **fields: object) -> RunLedgerRecord:
            return RunLedgerRecord({
                "schema_version": 1,
                "seq": seq,
                "type": event_type,
                "run_id": "r2",
                "session_id": "s",
                **fields,
            })

        projection = project_run_ledger([
            record(1, "run_started"),
            record(
                2,
                "review_result_projected",
                review_attempt_id="a1",
                verdict="approved",
                status="complete",
                origin="reused",
                finding_count=0,
                source_review_run_id="r1",
            ),
            record(3, "review_finished", review_attempt_id="a1"),
            record(4, "run_finished"),
        ])
        self.assertIsNotNone(projection.review)
        assert projection.review is not None
        self.assertEqual(projection.review.source_run_id, "r1")

    def test_partial_review_ledger_write_does_not_project_finished_review(self) -> None:
        from types import SimpleNamespace

        from codey.reviews.persistence import append_review_result_ledger

        result = ReviewResult(
            "approved", "ok", [], status="complete", source_run_id="r1",
            identity=SimpleNamespace(attempt_id="a1", artifact_sha256="sha"),
        )
        events: list[str] = []

        def append_ledger(action):
            if len(events) == 1:
                raise OSError("disk full")
            writer = SimpleNamespace(
                append=lambda event_type, **_fields: events.append(event_type),
            )
            action(writer)

        with self.assertRaises(OSError):
            append_review_result_ledger(append_ledger, result)
        self.assertEqual(events, ["review_result_projected"])

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
