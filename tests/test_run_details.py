from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from codey.runs.details import load_run_details, unavailable_summary
from codey.runs.ledger import RunLedgerStore
from codey.runs.trace import MAX_TRACE_BYTES, SCHEMA_VERSION, RunTraceStore


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
                receipt={"text": "1 file changed · checks passed", "changed_count": 1},
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
