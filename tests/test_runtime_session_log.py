from __future__ import annotations

import tempfile
import unittest
from dataclasses import dataclass, field
from pathlib import Path

from codey.runtime.operation import OperationContext, OperationIntent
from codey.runtime.outcome import OperationOutcome
from codey.runtime.reducer import reduce_session
from codey.runtime.scheduler import OperationScheduler
from codey.runtime import cancellation
from codey.runtime.session_log import (
    RuntimeLogCorruption,
    RuntimeLogEntry,
    RuntimeLogWriteError,
    RuntimeSessionLog,
)


class RuntimeSessionLogTests(unittest.TestCase):
    def test_append_and_reduce_operation_lifecycle(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            log = RuntimeSessionLog(Path(td))

            log.append(
                "s1",
                lane="current",
                operation_id="op-1",
                kind="operation_started",
                payload={"operation_kind": "agent"},
            )
            log.append(
                "s1",
                lane="current",
                operation_id="op-1",
                kind="operation_effect",
                payload={"effect_kind": "trace_ref", "ref": "trace:1"},
            )
            log.append(
                "s1",
                lane="current",
                operation_id="op-1",
                kind="operation_settled",
                payload={"outcome": "completed"},
            )

            projection = reduce_session(log.read("s1"))

        self.assertEqual(projection.lanes["current"].open_operation_id, "")
        self.assertEqual(projection.lanes["current"].settled_operation_ids, ["op-1"])
        self.assertEqual(projection.operations["op-1"].outcome, "completed")
        self.assertEqual(projection.operations["op-1"].effect_refs, ["trace:1"])

    def test_append_many_rows_share_one_complete_batch(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            log = RuntimeSessionLog(Path(td))

            rows = log.append_many(
                "s1",
                (
                    {
                        "lane": "current",
                        "operation_id": "op-1",
                        "kind": "operation_started",
                        "payload": {"operation_kind": "agent"},
                    },
                    {
                        "lane": "current",
                        "operation_id": "op-1",
                        "kind": "operation_settled",
                        "payload": {"outcome": "completed"},
                    },
                ),
            )

        self.assertEqual(len(rows), 2)
        self.assertEqual({row.batch_id for row in rows}, {rows[0].batch_id})
        self.assertEqual([row.batch_index for row in rows], [0, 1])
        self.assertEqual([row.batch_count for row in rows], [2, 2])

    def test_append_repairs_incomplete_tail_batch_before_new_write(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            log = RuntimeSessionLog(Path(td))
            log.append_many(
                "s1",
                (
                    {
                        "lane": "current",
                        "operation_id": "op-1",
                        "kind": "operation_started",
                        "payload": {"operation_kind": "agent"},
                    },
                    {
                        "lane": "current",
                        "operation_id": "op-1",
                        "kind": "operation_settled",
                        "payload": {"outcome": "completed"},
                    },
                ),
            )
            tail = RuntimeLogEntry(
                session_id="s1",
                lane="current",
                operation_id="op-tail",
                kind="operation_started",
                payload={"operation_kind": "agent"},
                batch_id="batch-tail",
                batch_index=0,
                batch_count=2,
            )
            path = log.path_for("s1")
            with path.open("ab") as handle:
                handle.write(tail.to_json_line().encode("utf-8"))

            self.assertEqual(
                [entry.operation_id for entry in log.read("s1")],
                ["op-1", "op-1"],
            )
            log.append(
                "s1",
                lane="current",
                operation_id="op-2",
                kind="operation_started",
                payload={"operation_kind": "agent"},
            )
            raw = path.read_text(encoding="utf-8")
            projection = reduce_session(log.read("s1"))

        self.assertNotIn("op-tail", raw)
        self.assertEqual(projection.lanes["current"].open_operation_id, "op-2")

    def test_append_repairs_partial_json_tail_before_new_write(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            log = RuntimeSessionLog(Path(td))
            log.append(
                "s1",
                lane="current",
                operation_id="op-1",
                kind="operation_started",
                payload={"operation_kind": "agent"},
            )
            path = log.path_for("s1")
            with path.open("ab") as handle:
                handle.write(b'{"schema_version":1')

            self.assertEqual(len(log.read("s1")), 1)
            log.append(
                "s1",
                lane="current",
                operation_id="op-1",
                kind="operation_settled",
                payload={"outcome": "aborted"},
            )
            projection = reduce_session(log.read("s1"))

        self.assertEqual(projection.operations["op-1"].outcome, "aborted")

    def test_rejects_second_open_operation_in_lane(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            log = RuntimeSessionLog(Path(td))
            log.append(
                "s1",
                lane="current",
                operation_id="op-1",
                kind="operation_started",
                payload={"operation_kind": "agent"},
            )

            with self.assertRaises(RuntimeLogCorruption):
                log.append(
                    "s1",
                    lane="current",
                    operation_id="op-2",
                    kind="operation_started",
                    payload={"operation_kind": "repair"},
                )

            self.assertEqual(len(log.read("s1")), 1)

    def test_rejects_duplicate_tool_invocation(self) -> None:
        entries = (
            RuntimeLogEntry(
                session_id="s1",
                lane="current",
                operation_id="op-1",
                kind="operation_started",
                payload={"operation_kind": "agent"},
            ),
            RuntimeLogEntry(
                session_id="s1",
                lane="current",
                operation_id="op-1",
                kind="tool_invocation",
                payload={"invocation_id": "tool-1"},
            ),
            RuntimeLogEntry(
                session_id="s1",
                lane="current",
                operation_id="op-1",
                kind="tool_invocation",
                payload={"invocation_id": "tool-1"},
            ),
        )

        with self.assertRaises(RuntimeLogCorruption):
            reduce_session(entries)

    def test_rejects_record_after_operation_settlement(self) -> None:
        entries = (
            RuntimeLogEntry(
                session_id="s1",
                lane="current",
                operation_id="op-1",
                kind="operation_started",
                payload={"operation_kind": "agent"},
            ),
            RuntimeLogEntry(
                session_id="s1",
                lane="current",
                operation_id="op-1",
                kind="operation_settled",
                payload={"outcome": "completed"},
            ),
            RuntimeLogEntry(
                session_id="s1",
                lane="current",
                operation_id="op-1",
                kind="operation_effect",
                payload={"effect_kind": "trace_ref", "ref": "trace:late"},
            ),
        )

        with self.assertRaises(RuntimeLogCorruption):
            reduce_session(entries)

    def test_unknown_effect_is_rejected(self) -> None:
        entries = (
            RuntimeLogEntry(
                session_id="s1",
                lane="current",
                operation_id="op-1",
                kind="operation_started",
                payload={"operation_kind": "agent"},
            ),
            RuntimeLogEntry(
                session_id="s1",
                lane="current",
                operation_id="op-1",
                kind="operation_effect",
                payload={"effect_kind": "future_effect", "ref": "future:1"},
                entry_id="entry-future",
            ),
        )

        with self.assertRaises(RuntimeLogCorruption):
            reduce_session(entries)

    def test_reducer_requires_started_kind_and_effect_ref(self) -> None:
        cases = (
            (
                RuntimeLogEntry(
                    session_id="s1",
                    lane="current",
                    operation_id="op-1",
                    kind="operation_started",
                    payload={},
                ),
            ),
            (
                RuntimeLogEntry(
                    session_id="s1",
                    lane="current",
                    operation_id="op-1",
                    kind="operation_started",
                    payload={"operation_kind": "agent"},
                ),
                RuntimeLogEntry(
                    session_id="s1",
                    lane="current",
                    operation_id="op-1",
                    kind="operation_effect",
                    payload={"effect_kind": "trace_ref"},
                ),
            ),
        )

        for entries in cases:
            with self.subTest(entries=entries):
                with self.assertRaises(RuntimeLogCorruption):
                    reduce_session(entries)

    def test_payload_rejects_raw_prompt_or_output_fields(self) -> None:
        with self.assertRaises(RuntimeLogWriteError):
            RuntimeLogEntry(
                session_id="s1",
                lane="current",
                operation_id="op-1",
                kind="operation_effect",
                payload={"raw_prompt": "do the thing"},
            )
        with self.assertRaises(RuntimeLogWriteError):
            RuntimeLogEntry(
                session_id="s1",
                lane="current",
                operation_id="op-1",
                kind="operation_effect",
                payload={"nested": {"stdout": "raw output"}},
            )

    def test_append_refuses_non_object_payload_without_coercion(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            log = RuntimeSessionLog(Path(td))

            with self.assertRaises(RuntimeLogCorruption):
                log.append(
                    "s1",
                    lane="current",
                    operation_id="op-1",
                    kind="operation_started",
                    payload=(("operation_kind", "agent"),),
                )

            self.assertEqual(log.read("s1"), ())

    def test_scheduler_records_started_and_settled_operation(self) -> None:
        @dataclass
        class FakeOperation:
            operation_id: str = "op-1"
            kind: str = "agent"
            lane: str = "current"
            intent: OperationIntent = field(
                default_factory=lambda: OperationIntent("task:1")
            )

            def run(self, context: OperationContext) -> OperationOutcome:
                self.seen_context = context
                return OperationOutcome.completed(summary="done")

        with tempfile.TemporaryDirectory() as td:
            log = RuntimeSessionLog(Path(td))
            operation = FakeOperation()

            outcome = OperationScheduler(log).run("s1", "run-1", operation)
            projection = reduce_session(log.read("s1"))

        self.assertEqual(outcome.status, "completed")
        self.assertEqual(operation.seen_context.run_id, "run-1")
        self.assertEqual(projection.operations["op-1"].outcome, "completed")

    def test_scheduler_settles_failed_operation_before_reraising(self) -> None:
        @dataclass
        class FailingOperation:
            operation_id: str = "op-1"
            kind: str = "agent"
            lane: str = "current"
            intent: OperationIntent = field(
                default_factory=lambda: OperationIntent("task:1")
            )

            def run(self, context: OperationContext) -> OperationOutcome:
                raise ValueError("bad operation")

        @dataclass
        class NextOperation:
            operation_id: str = "op-2"
            kind: str = "repair"
            lane: str = "current"
            intent: OperationIntent = field(
                default_factory=lambda: OperationIntent("task:2")
            )

            def run(self, context: OperationContext) -> OperationOutcome:
                return OperationOutcome.completed(summary="recovered")

        with tempfile.TemporaryDirectory() as td:
            log = RuntimeSessionLog(Path(td))
            scheduler = OperationScheduler(log)
            with self.assertRaises(ValueError):
                scheduler.run("s1", "run-1", FailingOperation())

            outcome = scheduler.run("s1", "run-2", NextOperation())
            projection = reduce_session(log.read("s1"))

        self.assertEqual(outcome.status, "completed")
        self.assertEqual(projection.operations["op-1"].outcome, "failed")
        self.assertEqual(projection.operations["op-2"].outcome, "completed")
        self.assertEqual(projection.lanes["current"].open_operation_id, "")

    def test_scheduler_settles_cancelled_operation_before_reraising(self) -> None:
        @dataclass
        class CancelledOperation:
            operation_id: str = "op-1"
            kind: str = "agent"
            lane: str = "current"
            intent: OperationIntent = field(
                default_factory=lambda: OperationIntent("task:1")
            )

            def run(self, context: OperationContext) -> OperationOutcome:
                raise cancellation.TaskCancelled("stopped")

        with tempfile.TemporaryDirectory() as td:
            log = RuntimeSessionLog(Path(td))
            with self.assertRaises(cancellation.TaskCancelled):
                OperationScheduler(log).run("s1", "run-1", CancelledOperation())
            projection = reduce_session(log.read("s1"))

        self.assertEqual(projection.operations["op-1"].outcome, "aborted")
        self.assertEqual(projection.lanes["current"].open_operation_id, "")

    def test_entry_payload_bad_created_at_reports_corruption(self) -> None:
        payload = {
            "schema_version": 1,
            "entry_id": "entry-1",
            "created_at": "not-a-number",
            "session_id": "s1",
            "lane": "current",
            "operation_id": "op-1",
            "kind": "operation_started",
            "payload": {"operation_kind": "agent"},
        }

        with self.assertRaises(RuntimeLogCorruption):
            RuntimeLogEntry.from_payload(payload)

    def test_entry_payload_is_closed_and_requires_durable_identity(self) -> None:
        valid = {
            "schema_version": 1,
            "entry_id": "entry-1",
            "created_at": 1.0,
            "session_id": "s1",
            "lane": "current",
            "operation_id": "op-1",
            "kind": "operation_started",
            "payload": {"operation_kind": "agent"},
            "batch_id": "batch-1",
            "batch_index": 0,
            "batch_count": 1,
        }
        invalid_cases = (
            {**valid, "raw_prompt": "drop"},
            {key: value for key, value in valid.items() if key != "entry_id"},
            {**valid, "entry_id": ""},
            {**valid, "created_at": 0},
            {key: value for key, value in valid.items() if key != "created_at"},
            {key: value for key, value in valid.items() if key != "batch_id"},
            {**valid, "batch_index": True},
            {**valid, "batch_count": 0},
        )

        for payload in invalid_cases:
            with self.subTest(payload=payload):
                with self.assertRaises(RuntimeLogCorruption):
                    RuntimeLogEntry.from_payload(payload)


if __name__ == "__main__":
    unittest.main()
