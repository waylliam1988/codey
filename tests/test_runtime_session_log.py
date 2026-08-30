from __future__ import annotations

import tempfile
import unittest
from dataclasses import dataclass, field
from pathlib import Path

from codey.runtime.operation import OperationContext, OperationIntent
from codey.runtime.outcome import OperationOutcome
from codey.runtime.reducer import reduce_session
from codey.runtime.scheduler import OperationScheduler
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

    def test_unknown_effect_is_not_replayed(self) -> None:
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

        projection = reduce_session(entries)

        self.assertEqual(projection.operations["op-1"].effect_refs, [])
        self.assertEqual(projection.ignored_effect_entry_ids, ("entry-future",))

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


if __name__ == "__main__":
    unittest.main()
