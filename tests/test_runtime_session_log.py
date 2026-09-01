from __future__ import annotations

import os
import tempfile
import unittest
from dataclasses import dataclass, field
from pathlib import Path

from codey.runtime.operation import OperationContext, OperationIntent
from codey.runtime.outcome import OperationOutcome
from codey.runtime.reducer import reduce_session
from codey.runtime.scheduler import OperationScheduler
from codey.runtime import cancellation
from codey.runtime.effect_records import (
    EFFECT_CATEGORY_TOOL_CALL,
    RuntimeEffectIntent,
    RuntimeEffectSettlement,
    RuntimeEffectStore,
)
from codey.runtime.effects import RuntimeOperationStore
from codey.runtime.replay_policy import ReplayClass
from codey.runtime.session_log import (
    RuntimeLogCorruption,
    RuntimeLogEntry,
    RuntimeLogWriteError,
    RuntimeSessionLog,
    _compact_entries,
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
                payload={"effect_kind": "run_phase", "ref": "phase:1"},
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
        self.assertEqual(projection.operations["op-1"].effect_refs, ["phase:1"])

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

    def test_projection_cache_replays_after_external_append_changes_file_size(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            log = RuntimeSessionLog(Path(td))
            log.append(
                "s1",
                lane="current",
                operation_id="op-1",
                kind="operation_started",
                payload={"operation_kind": "agent"},
            )
            cached = log.projection("s1")
            self.assertEqual(cached.lanes["current"].open_operation_id, "op-1")

            externally_written = RuntimeLogEntry(
                session_id="s1",
                lane="current",
                operation_id="op-1",
                kind="operation_settled",
                payload={"outcome": "completed"},
            )
            path = log.path_for("s1")
            with path.open("ab") as handle:
                handle.write(externally_written.to_json_line().encode("utf-8"))

            log.append(
                "s1",
                lane="current",
                operation_id="op-2",
                kind="operation_started",
                payload={"operation_kind": "agent"},
            )
            projection = log.projection("s1")

        self.assertEqual(projection.operations["op-1"].status, "settled")
        self.assertEqual(projection.lanes["current"].open_operation_id, "op-2")

    def test_projection_cache_replays_after_same_size_external_rewrite(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            log = RuntimeSessionLog(Path(td))
            log.append(
                "s1",
                lane="current",
                operation_id="op-1",
                kind="operation_started",
                payload={"operation_kind": "agent"},
            )
            self.assertEqual(
                log.projection("s1").lanes["current"].open_operation_id,
                "op-1",
            )
            path = log.path_for("s1")
            raw = path.read_text(encoding="utf-8")
            rewritten = raw.replace("op-1", "op-2")
            self.assertEqual(len(raw), len(rewritten))
            path.write_text(rewritten, encoding="utf-8")
            stat = path.stat()
            os.utime(path, ns=(stat.st_atime_ns, stat.st_mtime_ns + 1_000_000_000))

            projection = log.projection("s1")

        self.assertNotIn("op-1", projection.operations)
        self.assertEqual(projection.lanes["current"].open_operation_id, "op-2")

    def test_failed_append_validation_does_not_pollute_projection_cache(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            log = RuntimeSessionLog(Path(td))
            self.assertEqual(log.projection("s1").operations, {})

            with self.assertRaises(RuntimeLogCorruption):
                log.append(
                    "s1",
                    lane="current",
                    operation_id="op-missing",
                    kind="operation_settled",
                    payload={"outcome": "completed"},
                )

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
            projection = log.projection("s1")

        self.assertEqual(projection.operations["op-1"].outcome, "completed")
        self.assertEqual(projection.lanes["current"].open_operation_id, "")

    def test_append_uses_cache_after_compaction(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            log = RuntimeSessionLog(Path(td), max_log_bytes=1800)
            log.append(
                "s1",
                lane="current",
                operation_id="op-1",
                kind="operation_started",
                payload={"operation_kind": "task"},
            )
            for index in range(12):
                log.append(
                    "s1",
                    lane="current",
                    operation_id="op-1",
                    kind="operation_effect",
                    payload={
                        "effect_kind": "run_phase",
                        "ref": f"phase:{index}",
                        "padding": "x" * 80,
                    },
                )
            log.append(
                "s1",
                lane="current",
                operation_id="op-1",
                kind="operation_settled",
                payload={"outcome": "completed"},
            )
            compacted_entries = log.entries("s1")
            self.assertEqual(
                [entry.kind for entry in compacted_entries if entry.operation_id == "op-1"],
                ["operation_started", "operation_effect", "operation_settled"],
            )

            log.append(
                "s1",
                lane="current",
                operation_id="op-2",
                kind="operation_started",
                payload={"operation_kind": "agent"},
            )
            projection = log.projection("s1")

        self.assertEqual(projection.operations["op-1"].status, "settled")
        self.assertEqual(projection.lanes["current"].open_operation_id, "op-2")

    def test_compaction_retains_open_runtime_effect_recovery_facts(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            log = RuntimeSessionLog(Path(td), max_log_bytes=16000)
            operations = RuntimeOperationStore(log)
            effects = RuntimeEffectStore(log)
            started = operations.start(
                session_id="s1",
                run_id="run-1",
                project="",
                provider_id="deepseek",
                turn_budget=5,
                max_repair_rounds=1,
                task_kind="project",
            )
            assert started is not None

            read_id = "eff_read_compact"
            effects.record_intent(
                "s1",
                "run-1",
                RuntimeEffectIntent(
                    effect_id=read_id,
                    effect_category=EFFECT_CATEGORY_TOOL_CALL,
                    session_id="s1",
                    run_id="run-1",
                    turn=1,
                    tool_index=0,
                    tool_name="read",
                    replay_class=ReplayClass.SAFE,
                ),
            )
            effects.record_settlement(
                "s1",
                "run-1",
                RuntimeEffectSettlement(
                    effect_id=read_id,
                    effect_category=EFFECT_CATEGORY_TOOL_CALL,
                    session_id="s1",
                    run_id="run-1",
                    replay_class=ReplayClass.SAFE,
                ),
            )

            edit_id = "eff_edit_compact"
            effects.record_intent(
                "s1",
                "run-1",
                RuntimeEffectIntent(
                    effect_id=edit_id,
                    effect_category=EFFECT_CATEGORY_TOOL_CALL,
                    session_id="s1",
                    run_id="run-1",
                    turn=1,
                    tool_index=1,
                    tool_name="edit",
                    replay_class=ReplayClass.UNSAFE,
                ),
            )

            for index in range(30):
                log.append(
                    "s1",
                    lane=started.lane,
                    operation_id=started.operation_id,
                    kind="operation_effect",
                    payload={
                        "effect_kind": "run_phase",
                        "ref": f"phase:{index}",
                        "padding": "x" * 150,
                    },
                )

            recovered = RuntimeEffectStore(log).load_effects("s1", "run-1")

        by_id = {projection.intent.effect_id: projection for projection in recovered}
        self.assertFalse(by_id[read_id].is_pending)
        self.assertTrue(by_id[edit_id].is_pending)

    def test_compaction_treats_malformed_replay_count_as_not_recovered(self) -> None:
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
                payload={
                    "schema_version": 1,
                    "effect_kind": "runtime_effect",
                    "record_kind": "intent",
                    "ref": "effect:eff_bad_count",
                    "effect_id": "eff_bad_count",
                    "effect_category": "tool_call",
                    "session_id": "s1",
                    "run_id": "run-1",
                    "lane": "current",
                    "operation_id": "op-1",
                    "turn": 1,
                    "tool_index": 0,
                    "tool_name": "read",
                    "replay_class": "safe",
                },
            ),
            RuntimeLogEntry(
                session_id="s1",
                lane="current",
                operation_id="op-1",
                kind="operation_effect",
                payload={
                    "schema_version": 1,
                    "effect_kind": "runtime_effect",
                    "record_kind": "settlement",
                    "ref": "effect_settlement:eff_bad_count",
                    "effect_id": "eff_bad_count",
                    "effect_category": "tool_call",
                    "session_id": "s1",
                    "run_id": "run-1",
                    "lane": "current",
                    "operation_id": "op-1",
                    "status": "ok",
                    "sent_state": "settled",
                    "replay_class": "safe",
                    "replay_count": {"bad": True},
                },
            ),
            RuntimeLogEntry(
                session_id="s1",
                lane="current",
                operation_id="op-1",
                kind="operation_settled",
                payload={"outcome": "completed"},
            ),
        )

        compacted = _compact_entries(entries)

        effect_entries = [
            entry
            for entry in compacted
            if entry.payload.get("effect_id") == "eff_bad_count"
        ]
        self.assertEqual(effect_entries, [])

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

    def test_unwired_tool_entries_are_not_runtime_log_schema(self) -> None:
        for kind in ("tool_invocation", "tool_settled"):
            with self.subTest(kind=kind):
                with self.assertRaises(RuntimeLogCorruption):
                    RuntimeLogEntry(
                        session_id="s1",
                        lane="current",
                        operation_id="op-1",
                        kind=kind,
                        payload={"invocation_id": "tool-1"},
                    )

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
                payload={"effect_kind": "run_phase", "ref": "phase:late"},
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
                    payload={"effect_kind": "run_phase"},
                ),
            ),
        )

        for entries in cases:
            with self.subTest(entries=entries):
                with self.assertRaises(RuntimeLogCorruption):
                    reduce_session(entries)

    def test_append_compacts_phase_history_before_log_limit_bricks_session(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            log = RuntimeSessionLog(Path(td), max_log_bytes=1800)
            log.append(
                "s1",
                lane="current",
                operation_id="op-1",
                kind="operation_started",
                payload={"operation_kind": "task"},
            )
            for index in range(12):
                log.append(
                    "s1",
                    lane="current",
                    operation_id="op-1",
                    kind="operation_effect",
                    payload={
                        "effect_kind": "run_phase",
                        "ref": f"phase:{index}",
                        "padding": "x" * 80,
                    },
                )
            log.append(
                "s1",
                lane="current",
                operation_id="op-1",
                kind="operation_settled",
                payload={"outcome": "completed"},
            )
            log.append(
                "s1",
                lane="current",
                operation_id="op-2",
                kind="operation_started",
                payload={"operation_kind": "task"},
            )
            projection = reduce_session(log.read("s1"))
            kinds = [entry.kind for entry in log.read("s1") if entry.operation_id == "op-1"]

        self.assertEqual(kinds, ["operation_started", "operation_effect", "operation_settled"])
        self.assertEqual(projection.operations["op-1"].outcome, "completed")
        self.assertEqual(projection.lanes["current"].open_operation_id, "op-2")

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

    def test_scheduler_resumes_existing_open_operation_without_second_start(self) -> None:
        @dataclass
        class ResumedOperation:
            operation_id: str = "op-1"
            kind: str = "agent"
            lane: str = "current"
            intent: OperationIntent = field(
                default_factory=lambda: OperationIntent("task:1")
            )

            def run(self, context: OperationContext) -> OperationOutcome:
                self.seen_context = context
                return OperationOutcome.aborted(reason="stopped")

        with tempfile.TemporaryDirectory() as td:
            log = RuntimeSessionLog(Path(td))
            log.append(
                "s1",
                lane="current",
                operation_id="op-1",
                kind="operation_started",
                payload={"operation_kind": "agent"},
            )
            operation = ResumedOperation()

            outcome = OperationScheduler(log).run("s1", "run-1", operation)
            entries = log.read("s1")
            projection = reduce_session(entries)

        self.assertEqual(outcome.status, "aborted")
        self.assertEqual(operation.seen_context.run_id, "run-1")
        self.assertEqual(
            [entry.kind for entry in entries if entry.operation_id == "op-1"],
            ["operation_started", "operation_settled"],
        )
        self.assertEqual(projection.operations["op-1"].outcome, "aborted")

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
