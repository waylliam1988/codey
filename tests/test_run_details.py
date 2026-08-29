from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from codey.completion.edit_integrity import observe_edit_integrity
from codey.runs.details import load_run_details, unavailable_summary
from codey.runs.ledger import RunLedgerStore
from codey.runs.receipt import build_task_receipt
from codey.runs.trace import MAX_TRACE_BYTES, SCHEMA_VERSION, RunTraceStore


CLEAN_SOURCE_DIFF = (
    "diff --git a/src/mod.py b/src/mod.py\n"
    "--- a/src/mod.py\n"
    "+++ b/src/mod.py\n"
    "@@ -1 +1 @@\n"
    "-VALUE = 1\n"
    "+VALUE = 2\n"
)


def _clean_receipt(changed_count: int = 1) -> dict:
    observation = observe_edit_integrity(
        task="Change src/mod.py VALUE from 1 to 2.",
        changes={
            "changed_count": changed_count,
            "files": [{"path": "src/mod.py"}],
            "diff": CLEAN_SOURCE_DIFF,
        },
        diff=CLEAN_SOURCE_DIFF,
        files=("src/mod.py",),
        decision=None,
        run_id="run-1",
    )
    return build_task_receipt(
        {"mode": "snapshot", "changed_count": changed_count},
        integrity=observation,
        checks_passed=True,
    ).to_dict()


class RunDetailsTests(unittest.TestCase):
    def test_load_run_details_projects_bounded_user_summary(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            state = Path(td)
            ledger_store = RunLedgerStore(state)
            trace_store = RunTraceStore(state)
            writer = ledger_store.open(
                run_id="run-1",
                session_id="session-1",
                project=state / "project",
                task="SECRET_TASK_SHOULD_NOT_APPEAR",
                provider="deepseek",
                mode="project",
            )
            writer.append("tool_finished", tool="read", ok=True)
            writer.append("tool_finished", tool="search", ok=True)
            writer.append("tool_finished", tool="edit", ok=True)
            writer.append("command_verified", command="pytest SECRET_STDOUT", cwd=".")
            writer.append_changes_collected(
                {"ok": True, "mode": "snapshot", "changed_count": 1, "files": []},
                checks_passed=True,
                receipt=_clean_receipt(),
            )
            writer.finish(
                summary="SECRET_SUMMARY_SHOULD_NOT_APPEAR",
                stop_reason="done",
                turns=3,
                max_turns=8,
                provider="deepseek",
            )
            trace = trace_store.open(
                run_id="run-1",
                session_id="session-1",
                project=state / "project",
                mode_initial="project",
                provider_initial="deepseek",
            )
            trace.record_local_context_refs(({"id": "pref-1", "scope": "session", "kind": "preference"},))
            trace.record_research_notes(("note-1",))
            trace.record_research_sources(({"requested_url": "https://example.com/a"},))
            trace.record_policy_decision({
                "kind": "run_command",
                "decision": "deny",
                "guard_id": "run_command_guard",
                "reason_code": "command_not_allowed",
                "phase": "writer",
                "subject_ref": "action:" + ("a" * 64),
                "display_digest": "sha256:" + ("b" * 64),
            })
            trace.finish(status="done", mode="project", provider="deepseek")

            summary = load_run_details(
                run_ledgers=ledger_store,
                run_traces=trace_store,
                session_id="session-1",
                run_id="run-1",
            )
            payload = summary.to_jsonable()
            serialized = json.dumps(payload, ensure_ascii=False)

        self.assertTrue(summary.available)
        rows = {row["label"]: row for row in payload["rows"]}
        self.assertEqual(rows["Work"]["value"], "Project writing")
        self.assertEqual(rows["Model"]["value"], "DeepSeek")
        self.assertIn("Project context", rows["Context"]["value"])
        self.assertIn("Local context ref (1)", rows["Context"]["value"])
        self.assertIn("Research source (1)", rows["Context"]["value"])
        self.assertIn("Research note (1)", rows["Context"]["value"])
        self.assertIn("edited 1 file", rows["Actions"]["value"])
        self.assertIn("inspected 2 items", rows["Actions"]["value"])
        self.assertIn("ran 1 check", rows["Actions"]["value"])
        self.assertEqual(rows["Safety"]["value"], "blocked 1 action")
        self.assertEqual(rows["Safety"]["tone"], "warning")
        self.assertEqual(rows["Model fallback"]["value"], "None")
        self.assertEqual(rows["Verification"]["value"], "Checks passed")
        for secret in (
            "SECRET_TASK_SHOULD_NOT_APPEAR",
            "SECRET_STDOUT",
            "SECRET_SUMMARY_SHOULD_NOT_APPEAR",
            "https://example.com/a",
            "RunTrace",
            "PromptEnvelope",
            "Policy Pipeline",
            "Router",
            "Ghost",
            "Hebbian",
            "Directive",
            "Provider",
        ):
            self.assertNotIn(secret, serialized)

    def test_verification_row_never_reconstructs_green_without_receipt(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            state = Path(td)
            ledger_store = RunLedgerStore(state)
            writer = ledger_store.open(
                run_id="run-legacy",
                session_id="session-legacy",
                project=state / "project",
                task="task",
                provider="deepseek",
                mode="project",
            )
            writer.append_changes_collected(
                {"ok": True, "mode": "snapshot", "changed_count": 2, "files": []},
                checks_passed=True,
                # A legacy-shaped receipt fails closed: no claim may be
                # derived from the stored checks_passed fact either.
                receipt={"text": "2 files changed · checks passed", "changed_count": 2},
            )
            writer.finish(
                summary="done",
                stop_reason="done",
                turns=1,
                max_turns=8,
                provider="deepseek",
            )

            rows = {
                row["label"]: row
                for row in load_run_details(
                    run_ledgers=ledger_store,
                    run_traces=RunTraceStore(state),
                    session_id="session-legacy",
                    run_id="run-legacy",
                ).to_jsonable()["rows"]
            }

        self.assertEqual(rows["Verification"]["value"], "Checks not recorded")
        self.assertEqual(rows["Verification"]["tone"], "warning")

    def test_malformed_schema_v1_receipt_never_shows_checks_passed(self) -> None:
        # A schema-v1 payload whose trust contradicts its own facts fails
        # the reader contract: Details must not greet it with a green row.
        with tempfile.TemporaryDirectory() as td:
            state = Path(td)
            ledger_store = RunLedgerStore(state)
            writer = ledger_store.open(
                run_id="run-malformed",
                session_id="session-malformed",
                project=state / "project",
                task="task",
                provider="deepseek",
                mode="project",
            )
            # Raw append: the malformed receipt reaches the reader exactly
            # the way a tampered ledger would.
            writer.append(
                "changes_collected",
                ok=True,
                mode="git",
                changed_count=2,
                files=[],
                checks_passed=True,
                receipt={
                    "schema_version": 1,
                    "display": {
                        "summary": "2 files changed · checks passed",
                        "detail": "",
                    },
                    "work": {
                        "changed_count": 2,
                        "mode": "git",
                        "restore_available": False,
                    },
                    "verification": {"trust": "trusted", "checks_passed": True},
                    "integrity": {"status": "unobserved", "severity": "none"},
                },
            )
            writer.finish(
                summary="done",
                stop_reason="done",
                turns=1,
                max_turns=8,
                provider="deepseek",
            )

            rows = {
                row["label"]: row
                for row in load_run_details(
                    run_ledgers=ledger_store,
                    run_traces=RunTraceStore(state),
                    session_id="session-malformed",
                    run_id="run-malformed",
                ).to_jsonable()["rows"]
            }

        self.assertEqual(rows["Verification"]["value"], "Checks not recorded")
        self.assertEqual(rows["Verification"]["tone"], "warning")

    def test_load_run_details_reports_quiet_unavailable_when_missing(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            summary = load_run_details(
                run_ledgers=RunLedgerStore(td),
                run_traces=RunTraceStore(td),
                session_id="session-missing",
                run_id="run-missing",
            )

        self.assertFalse(summary.available)
        self.assertEqual(summary.to_jsonable()["rows"][0]["value"], "Details unavailable")

    def test_missing_ids_return_unavailable_without_store_reads(self) -> None:
        summary = load_run_details(
            run_ledgers=object(),
            run_traces=object(),
            session_id="",
            run_id="",
        )

        self.assertFalse(summary.available)
        self.assertEqual(summary, unavailable_summary())

    def test_run_details_fallback_and_approval_copy_are_user_facing(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            state = Path(td)
            ledger_store = RunLedgerStore(state)
            trace_store = RunTraceStore(state)
            writer = ledger_store.open(
                run_id="run-2",
                session_id="session-2",
                project="",
                task="hello",
                provider="qwen",
                mode="chat",
            )
            writer.finish(stop_reason="done", turns=1, max_turns=8, provider="stepfun")
            trace = trace_store.open(
                run_id="run-2",
                session_id="session-2",
                project=None,
                mode_initial="chat",
                provider_initial="qwen",
            )
            trace.record_fallback(
                from_provider="qwen",
                to_provider="stepfun",
                phase="chat",
                reason_code="connect_failed",
            )
            trace.record_policy_decision({
                "kind": "shell",
                "decision": "ask_user",
                "guard_id": "shell_approval_guard",
                "reason_code": "approval_required",
                "phase": "writer",
                "subject_ref": "action:" + ("c" * 64),
                "display_digest": "sha256:" + ("d" * 64),
            })
            trace.finish(status="done", mode="chat", provider="stepfun")

            payload = load_run_details(
                run_ledgers=ledger_store,
                run_traces=trace_store,
                session_id="session-2",
                run_id="run-2",
            ).to_jsonable()

        rows = {row["label"]: row for row in payload["rows"]}
        self.assertEqual(rows["Model fallback"]["value"], "Qwen -> StepFun")
        self.assertEqual(rows["Safety"]["value"], "asked for approval 1 time")
        self.assertNotIn("provider", json.dumps(payload))

    def test_operation_state_adds_one_quiet_progress_row_when_interrupted(self) -> None:
        from codey.run_operation import (
            RunOperationStore,
            mark_terminal,
            mark_writer_running,
        )

        with tempfile.TemporaryDirectory() as td:
            state = Path(td)
            ledger_store = RunLedgerStore(state)
            trace_store = RunTraceStore(state)
            store = RunOperationStore(state)
            started = store.start(
                session_id="session-progress",
                run_id="run-progress",
                project="",
                provider_id="deepseek",
                turn_budget=6,
                max_repair_rounds=1,
            )
            self.assertIsNotNone(started)
            store.commit(
                "session-progress",
                "run-progress",
                lambda s: mark_writer_running(s, provider_id="deepseek"),
            )
            ledger_store.open(
                run_id="run-progress",
                session_id="session-progress",
                project="",
                task="",
                provider="deepseek",
                mode="project",
            )

            rows = {
                row["label"]: row
                for row in load_run_details(
                    run_ledgers=ledger_store,
                    run_traces=trace_store,
                    run_operations=store,
                    session_id="session-progress",
                    run_id="run-progress",
                ).to_jsonable()["rows"]
            }
            self.assertEqual(rows["Progress"]["value"], "Writing was interrupted")
            self.assertEqual(rows["Progress"]["tone"], "warning")

            # Terminal: the run finished normally, so no Progress row.
            store.commit(
                "session-progress",
                "run-progress",
                lambda s: mark_terminal(
                    s,
                    stop_reason="done",
                    summary_chars=3,
                    turns=2,
                    max_turns=6,
                    provider="deepseek",
                ),
            )
            rows = {
                row["label"]: row
                for row in load_run_details(
                    run_ledgers=ledger_store,
                    run_traces=trace_store,
                    run_operations=store,
                    session_id="session-progress",
                    run_id="run-progress",
                ).to_jsonable()["rows"]
            }
            self.assertNotIn("Progress", rows)

    def test_trace_read_is_bounded_and_schema_checked(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            state = Path(td)
            ledger_store = RunLedgerStore(state)
            trace_store = RunTraceStore(state)
            writer = ledger_store.open(
                run_id="run-large-trace",
                session_id="session-large-trace",
                project="",
                task="hello",
                provider="deepseek",
                mode="chat",
            )
            writer.finish(stop_reason="done", turns=1, max_turns=8, provider="deepseek")
            trace_path = trace_store.path_for("session-large-trace", "run-large-trace")
            trace_path.parent.mkdir(parents=True, exist_ok=True)
            trace_path.write_text("{" + (" " * MAX_TRACE_BYTES) + "}", encoding="utf-8")

            payload = load_run_details(
                run_ledgers=ledger_store,
                run_traces=trace_store,
                session_id="session-large-trace",
                run_id="run-large-trace",
            ).to_jsonable()
            rows = {row["label"]: row for row in payload["rows"]}

            self.assertEqual(rows["Context"]["value"], "No extra context recorded")
            self.assertEqual(rows["Work"]["value"], "Chat")
            self.assertEqual(rows["Model"]["value"], "DeepSeek")

            trace_path.write_text(
                json.dumps({"schema_version": 999, "kind": "run_trace_manifest"}),
                encoding="utf-8",
            )
            payload = load_run_details(
                run_ledgers=ledger_store,
                run_traces=trace_store,
                session_id="session-large-trace",
                run_id="run-large-trace",
            ).to_jsonable()
            rows = {row["label"]: row for row in payload["rows"]}

            self.assertEqual(rows["Context"]["value"], "No extra context recorded")

            trace_path.write_text(
                json.dumps({
                    "schema_version": SCHEMA_VERSION,
                    "kind": "not_run_trace_manifest",
                    "prompt_sections": [{"name": "bad"}],
                }),
                encoding="utf-8",
            )
            payload = load_run_details(
                run_ledgers=ledger_store,
                run_traces=trace_store,
                session_id="session-large-trace",
                run_id="run-large-trace",
            ).to_jsonable()
            rows = {row["label"]: row for row in payload["rows"]}

        self.assertEqual(rows["Context"]["value"], "No extra context recorded")
        self.assertEqual(rows["Work"]["value"], "Chat")


if __name__ == "__main__":
    unittest.main()
