from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from codey.runs.ledger import SCHEMA_VERSION, RunLedgerRecord, RunLedgerStore
from codey.runs.ledger_projection import (
    ChangesSummary,
    RunLedgerProjection,
    build_task_receipt_from_projection,
    load_run_projection,
    project_run_ledger,
)
from codey.completion.edit_integrity import observe_edit_integrity
from codey.runs.receipt import build_task_receipt


def _record(seq: int, event_type: str, **fields: object) -> RunLedgerRecord:
    return RunLedgerRecord({
        "schema_version": SCHEMA_VERSION,
        "seq": seq,
        "ts": f"2026-07-28T00:00:{seq:02d}Z",
        "type": event_type,
        "run_id": "run-1",
        "session_id": "session-1",
        **fields,
    })


def _receipt_payload(changed_count: int = 2) -> dict:
    observation = observe_edit_integrity(
        task="Change src/mod.py VALUE from 1 to 2.",
        changes={"changed_count": changed_count, "files": [{"path": "src/mod.py"}], "diff": ""},
        diff="",
        files=("src/mod.py",),
        decision=None,
        run_id="run-1",
    )
    return build_task_receipt(
        {"mode": "snapshot", "changed_count": changed_count},
        integrity=observation,
        checks_passed=True,
    ).to_dict()


class RunLedgerProjectionTests(unittest.TestCase):
    def test_projects_core_run_facts_and_round_trips_schema_v1_receipt(self) -> None:
        records = [
            _record(
                10,
                "changes_collected",
                ok=True,
                mode="snapshot",
                changed_count=2,
                files=[
                    {"path": "app.py", "status": "M", "additions": 3, "deletions": 1},
                    {"path": "tests/test_app.py", "status": "A", "additions": 5, "deletions": 0},
                ],
                files_truncated=False,
                checks_passed=True,
                receipt=_receipt_payload(changed_count=2),
            ),
            _record(1, "run_started", project="E:/project", mode="project", provider="deepseek", task_chars=8),
            _record(2, "provider_selected", provider="deepseek"),
            _record(3, "model_reply", reply_chars=40),
            _record(4, "model_reply", reply_chars=2),
            _record(5, "tool_finished", tool="edit", ok=True),
            _record(6, "tool_finished", tool="run", ok=False),
            _record(7, "file_changed", path="app.py"),
            _record(8, "command_verified", command="python -m pytest -q", cwd=".", turn=2, tool_id="2:0"),
            _record(9, "provider_failure", provider="deepseek", action="task", kind="transient", message="timeout"),
            _record(11, "provider_switched", from_provider="deepseek", to_provider="qwen", phase="writer_failover", reason="provider_failure"),
            _record(12, "run_finished", provider="qwen", stop_reason="done", turns=2, max_turns=8),
        ]

        projection = project_run_ledger(records)
        receipt = build_task_receipt_from_projection(projection)

        self.assertTrue(projection.complete)
        self.assertEqual(projection.run_id, "run-1")
        self.assertEqual(projection.session_id, "session-1")
        self.assertEqual(projection.project, "E:/project")
        self.assertEqual(projection.provider_initial, "deepseek")
        self.assertEqual(projection.provider_final, "qwen")
        self.assertEqual(projection.model_reply_count, 2)
        self.assertEqual(projection.model_reply_chars, 42)
        self.assertEqual(projection.tool_calls, 2)
        self.assertEqual(projection.tool_errors, 1)
        self.assertEqual(projection.tool_counts, {"edit": 1, "run": 1})
        self.assertEqual(projection.changed_files_observed, ("app.py",))
        self.assertEqual(len(projection.verified_commands), 1)
        self.assertEqual(len(projection.provider_failures), 1)
        self.assertEqual(len(projection.provider_switches), 1)
        self.assertIsNotNone(projection.final_changes)
        self.assertEqual(projection.final_changes.changed_count, 2)
        self.assertEqual(len(projection.final_changes.files), 2)
        self.assertIsNotNone(receipt)
        self.assertEqual(
            receipt.display.summary,
            "2 files changed \u00b7 checks passed",
        )
        self.assertTrue(receipt.work.restore_available)
        self.assertTrue(receipt.verification.checks_passed)

    def test_malformed_stored_receipt_projects_to_none(self) -> None:
        # A legacy-shaped or junk receipt row must fail closed: the
        # projection never rebuilds a half-valid receipt from it.
        records = [
            _record(1, "run_started", provider="deepseek"),
            _record(
                2,
                "changes_collected",
                mode="snapshot",
                changed_count=2,
                checks_passed=True,
                receipt={"changed_count": 99, "checks_passed": False},
            ),
        ]

        projection = project_run_ledger(records)

        self.assertIsNotNone(projection.final_changes)
        self.assertIsNone(projection.final_changes.receipt)
        self.assertIsNone(build_task_receipt_from_projection(projection))

    def test_last_changes_win_and_verified_commands_are_deduplicated(self) -> None:
        records = [
            _record(1, "run_started", provider="deepseek"),
            _record(2, "command_verified", command="pytest", cwd=".", turn=1, tool_id="1:0"),
            _record(3, "command_verified", command="pytest", cwd=".", turn=2, tool_id="2:0"),
            _record(4, "file_changed", path="app.py"),
            _record(5, "file_changed", path="app.py"),
            _record(
                6,
                "changes_collected",
                mode="snapshot",
                changed_count=2,
                checks_passed=True,
                receipt=_receipt_payload(),
            ),
            _record(7, "changes_collected", mode="none", changed_count=0, checks_passed=False),
            _record(8, "run_finished", provider="deepseek", stop_reason="done"),
        ]

        projection = project_run_ledger(records)

        self.assertEqual(len(projection.verified_commands), 1)
        self.assertEqual(projection.changed_files_observed, ("app.py",))
        self.assertIsNotNone(projection.final_changes)
        self.assertEqual(projection.final_changes.changed_count, 0)
        self.assertFalse(projection.final_changes.checks_passed)
        # The last collection wins, including its receipt.
        self.assertIsNone(projection.final_changes.receipt)

    def test_ignores_unknown_future_and_malformed_records(self) -> None:
        records = [
            RunLedgerRecord({"schema_version": SCHEMA_VERSION + 1, "seq": 1, "type": "run_started"}),
            RunLedgerRecord({"schema_version": SCHEMA_VERSION, "seq": 2, "type": "unknown_event"}),
            RunLedgerRecord({"schema_version": SCHEMA_VERSION, "seq": 3}),
            _record(4, "run_started", provider="deepseek"),
            _record(5, "run_finished", provider="deepseek", stop_reason="done"),
        ]

        projection = project_run_ledger(records)

        self.assertTrue(projection.complete)
        self.assertEqual(projection.provider_initial, "deepseek")
        self.assertEqual(projection.stop_reason, "done")

    def test_receipt_projection_is_none_without_change_collection(self) -> None:
        projection = RunLedgerProjection(
            final_changes=ChangesSummary(
                ok=True,
                mode="snapshot",
                changed_count=1,
                files=(),
                files_truncated=False,
                checks_passed=True,
            ),
            has_run_started=True,
            has_run_finished=True,
        )

        self.assertIsNone(build_task_receipt_from_projection(projection))
        self.assertIsNone(build_task_receipt_from_projection(None))

    def test_load_run_projection_returns_none_for_empty_or_unreadable_ledgers(self) -> None:
        class BadStore:
            def path_for(self, _session_id: str, _run_id: str) -> Path:
                raise OSError("bad path")

        with tempfile.TemporaryDirectory() as td:
            store = RunLedgerStore(td)

            self.assertIsNone(load_run_projection(store, "session", "missing"))
            self.assertIsNone(load_run_projection(BadStore(), "session", "run"))  # type: ignore[arg-type]


if __name__ == "__main__":
    unittest.main()
