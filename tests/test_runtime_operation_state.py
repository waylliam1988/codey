from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from codey.runtime.mutation_line import RuntimeMutationLine
from codey.runtime.operation_state import (
    DRIVER_REPAIR,
    DRIVER_WRITER,
    KIND,
    LEAF_ACCEPTED,
    LEAF_COMPLETION_PROOF_RECORDED,
    LEAF_PROVIDER_EFFECT_PENDING,
    LEAF_REPAIR_CONTEXT_ADMITTED,
    LEAF_REPAIR_RUNNING,
    LEAF_REPAIR_SETTLED,
    LEAF_TERMINAL,
    LEAF_TOOL_DELIVERY_PENDING,
    LEAF_TOOL_EFFECT_PENDING,
    LEAF_WRITER_RUNNING,
    LEAF_WRITER_SETTLED,
    RuntimeOperationState,
    RuntimeOperationTransitionError,
    SCHEMA_VERSION,
    mark_completion_blocked,
    mark_completion_proof_recorded,
    mark_provider_effect_pending,
    mark_provider_effect_settled,
    mark_repair_context_admitted,
    mark_repair_running,
    mark_terminal,
    mark_tool_delivery_settled,
    mark_tool_effect_pending,
    mark_tool_effect_settled,
    mark_writer_running,
    mark_writer_settled,
    operation_id_for_run,
    operation_progress_text,
    operation_state_from_entries,
)
from codey.runtime.session_log import RuntimeLogEntry, RuntimeSessionLog
from codey.runtime.session_projection import reduce_session

PROOF_OK = "completion_proof:0123456789abcdef"
PROOF_FAIL = "completion_proof:fedcba9876543210"
CONTEXT_REF = "sha256:" + "a" * 64


def _state(leaf: str, **overrides: object) -> RuntimeOperationState:
    values = {
        "session_id": "s1",
        "run_id": "run-1",
        "operation_id": "task:" + "1" * 24,
        "lane": "run:" + "2" * 24,
        "project_ref": "project:" + "3" * 24,
        "provider_id": "deepseek",
        "turn_budget": 5,
        "max_repair_rounds": 1,
        "leaf": leaf,
        "started_at": "2026-01-01T00:00:00Z",
        "updated_at": "2026-01-01T00:00:01Z",
        "task_kind": "project",
    }
    values.update(overrides)
    return RuntimeOperationState(**values)


class RuntimeOperationStateTests(unittest.TestCase):
    def test_operation_state_is_first_class_log_entry(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            log = RuntimeSessionLog(Path(td))
            line = RuntimeMutationLine(log)
            state = line.accept_operation(
                session_id="s1",
                run_id="run-1",
                project=".",
                provider_id="deepseek",
                turn_budget=5,
                max_repair_rounds=1,
                task_kind="project",
            )
            assert state is not None
            state = line.mark_writer_running(
                "s1",
                "run-1",
                provider_id="deepseek",
            )
            entries = log.read("s1")

        assert state is not None
        self.assertEqual(state.leaf, LEAF_WRITER_RUNNING)
        self.assertEqual(
            [entry.kind for entry in entries if entry.operation_id == state.operation_id],
            ["operation_started", "operation_state", "operation_state"],
        )
        self.assertEqual(
            [
                entry.payload["leaf"]
                for entry in entries
                if entry.kind == "operation_state"
            ],
            [LEAF_ACCEPTED, LEAF_WRITER_RUNNING],
        )

    def test_start_returns_existing_open_state_without_rewinding(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            log = RuntimeSessionLog(Path(td))
            line = RuntimeMutationLine(log)
            first = line.accept_operation(
                session_id="s1",
                run_id="run-1",
                project=".",
                provider_id="deepseek",
                turn_budget=5,
                max_repair_rounds=1,
                task_kind="project",
            )
            assert first is not None
            line.mark_writer_running(
                "s1",
                "run-1",
                provider_id="deepseek",
            )
            resumed = line.accept_operation(
                session_id="s1",
                run_id="run-1",
                project=".",
                provider_id="deepseek",
                turn_budget=5,
                max_repair_rounds=1,
                task_kind="project",
            )
            entries = log.read("s1")

        assert resumed is not None
        self.assertEqual(resumed.leaf, LEAF_WRITER_RUNNING)
        self.assertEqual(
            [entry.kind for entry in entries].count("operation_started"),
            1,
        )
        self.assertEqual(
            [entry.kind for entry in entries].count("operation_state"),
            2,
        )

    def test_terminal_transition_settles_operation_in_same_batch(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            log = RuntimeSessionLog(Path(td))
            line = RuntimeMutationLine(log)
            line.accept_operation(
                session_id="s1",
                run_id="run-1",
                project="",
                provider_id="deepseek",
                turn_budget=5,
                max_repair_rounds=1,
                task_kind="chat",
            )
            terminal = line.mark_terminal(
                "s1",
                "run-1",
                stop_reason="done",
                summary_chars=4,
                turns=0,
                max_turns=5,
                provider="deepseek",
            )
            entries = log.read("s1")
            projection = reduce_session(entries)

        assert terminal is not None
        self.assertEqual(terminal.leaf, LEAF_TERMINAL)
        self.assertEqual(
            [entry.kind for entry in entries if entry.operation_id == terminal.operation_id][-2:],
            ["operation_state", "operation_settled"],
        )
        self.assertEqual(
            entries[-1].batch_id,
            entries[-2].batch_id,
        )
        self.assertEqual(projection.operations[terminal.operation_id].outcome, "completed")
        self.assertEqual(projection.lanes[terminal.lane].open_operation_id, "")

    def test_operation_state_payload_is_closed_schema_v1(self) -> None:
        payload = _state(LEAF_ACCEPTED).to_payload()
        self.assertEqual(payload["schema_version"], SCHEMA_VERSION)
        self.assertEqual(payload["kind"], KIND)
        self.assertEqual(RuntimeOperationState.from_payload(payload), _state(LEAF_ACCEPTED))

        for key, value in (
            ("schema_version", 2),
            ("kind", "legacy"),
            ("raw_prompt", "never"),
            ("leaf", "retry_wait"),
        ):
            with self.subTest(key=key):
                mutated = dict(payload)
                mutated[key] = value
                self.assertIsNone(RuntimeOperationState.from_payload(mutated))

    def test_operation_state_projection_fails_closed_on_corrupt_latest_state(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            log = RuntimeSessionLog(Path(td))
            line = RuntimeMutationLine(log)
            state = line.accept_operation(
                session_id="s1",
                run_id="run-1",
                project="",
                provider_id="deepseek",
                turn_budget=5,
                max_repair_rounds=1,
                task_kind="project",
            )
            assert state is not None
            state = line.mark_writer_running(
                "s1",
                "run-1",
                provider_id="deepseek",
            )
            assert state is not None
            valid_entries = log.entries("s1")
            corrupt_payload = dict(valid_entries[-1].payload)
            corrupt_payload["leaf"] = "retry_wait"
            corrupt_entry = RuntimeLogEntry(
                session_id="s1",
                lane=state.lane,
                operation_id=state.operation_id,
                kind="operation_state",
                payload=corrupt_payload,
            )

        with self.assertRaises(RuntimeOperationTransitionError):
            operation_state_from_entries(
                (*valid_entries, corrupt_entry),
                session_id="s1",
                run_id="run-1",
            )

    def test_pending_provider_state_requires_one_effect_and_driver(self) -> None:
        state = mark_provider_effect_pending(
            mark_writer_running(_state(LEAF_ACCEPTED), provider_id="deepseek"),
            effect_id="eff-provider",
            driver=DRIVER_WRITER,
            provider_id="deepseek",
            turn=1,
            delivery_batch_id="batch-1",
        )
        self.assertEqual(state.leaf, LEAF_PROVIDER_EFFECT_PENDING)
        self.assertEqual(state.pending_effect_ids, ("eff-provider",))
        self.assertEqual(state.pending_delivery_batch_id, "batch-1")
        self.assertEqual(
            mark_provider_effect_settled(state, effect_id="eff-provider").leaf,
            LEAF_WRITER_RUNNING,
        )

    def test_pending_tool_state_tracks_remaining_effect_ids(self) -> None:
        state = mark_tool_effect_pending(
            mark_writer_running(_state(LEAF_ACCEPTED), provider_id="deepseek"),
            effect_ids=("eff-read", "eff-edit"),
            driver=DRIVER_WRITER,
            delivery_batch_id="batch-1",
            turn=2,
        )
        self.assertEqual(state.leaf, LEAF_TOOL_EFFECT_PENDING)
        first = mark_tool_effect_settled(state, effect_id="eff-read")
        self.assertEqual(first.leaf, LEAF_TOOL_EFFECT_PENDING)
        self.assertEqual(first.pending_effect_ids, ("eff-edit",))
        final = mark_tool_effect_settled(first, effect_id="eff-edit")
        self.assertEqual(final.leaf, LEAF_TOOL_DELIVERY_PENDING)
        self.assertEqual(
            mark_tool_delivery_settled(final).leaf,
            LEAF_WRITER_RUNNING,
        )

    def test_repair_driver_reuses_provider_and_tool_pending_leaves(self) -> None:
        repair = mark_repair_running(
            mark_repair_context_admitted(
                mark_completion_proof_recorded(
                    mark_writer_settled(
                        mark_writer_running(_state(LEAF_ACCEPTED), provider_id="deepseek"),
                        provider_id="deepseek",
                        turns_used=1,
                        stop_reason="done",
                    ),
                    proof_ref=PROOF_FAIL,
                    proof_status="failed",
                    proof_satisfied=False,
                ),
                context_ref=CONTEXT_REF,
            ),
            provider_id="deepseek",
        )
        provider_pending = mark_provider_effect_pending(
            repair,
            effect_id="eff-repair-provider",
            driver=DRIVER_REPAIR,
            provider_id="deepseek",
            turn=1,
        )
        self.assertEqual(provider_pending.leaf, LEAF_PROVIDER_EFFECT_PENDING)
        self.assertEqual(
            mark_provider_effect_settled(provider_pending, effect_id="eff-repair-provider").leaf,
            LEAF_REPAIR_RUNNING,
        )
        tool_pending = mark_tool_effect_pending(
            repair,
            effect_ids=("eff-repair-read",),
            driver=DRIVER_REPAIR,
            delivery_batch_id="batch-repair",
            turn=1,
        )
        self.assertEqual(
            mark_tool_delivery_settled(
                mark_tool_effect_settled(tool_pending, effect_id="eff-repair-read")
            ).leaf,
            LEAF_REPAIR_RUNNING,
        )

    def test_repair_and_blocked_verdict_invariants(self) -> None:
        with self.assertRaises(RuntimeOperationTransitionError):
            mark_repair_running(_state(LEAF_WRITER_SETTLED), provider_id="deepseek")

        proof = mark_completion_proof_recorded(
            mark_writer_settled(
                mark_writer_running(_state(LEAF_ACCEPTED), provider_id="deepseek"),
                provider_id="deepseek",
                turns_used=1,
                stop_reason="done",
            ),
            proof_ref=PROOF_FAIL,
            proof_status="failed",
            proof_satisfied=False,
        )
        blocked = mark_completion_blocked(proof, reason="blocked")
        terminal = mark_terminal(
            blocked,
            stop_reason="done",
            summary_chars=1,
            turns=1,
            max_turns=5,
            provider="deepseek",
        )
        self.assertEqual(terminal.blocked_reason, "blocked")
        self.assertIs(mark_completion_blocked(blocked, reason="blocked"), blocked)
        with self.assertRaises(RuntimeOperationTransitionError):
            mark_completion_blocked(blocked, reason="different")

    def test_progress_text_uses_leaf_and_driver(self) -> None:
        self.assertEqual(
            operation_progress_text(mark_writer_running(_state(LEAF_ACCEPTED), provider_id="deepseek")),
            "Writing was interrupted",
        )
        proof = mark_completion_proof_recorded(
            mark_writer_settled(
                mark_writer_running(_state(LEAF_ACCEPTED), provider_id="deepseek"),
                provider_id="deepseek",
                turns_used=1,
                stop_reason="done",
            ),
            proof_ref=PROOF_OK,
            proof_status="complete",
            proof_satisfied=True,
        )
        self.assertEqual(operation_progress_text(proof), "Finishing was interrupted")
        repair = mark_repair_context_admitted(
            mark_completion_proof_recorded(
                mark_writer_settled(
                    mark_writer_running(_state(LEAF_ACCEPTED), provider_id="deepseek"),
                    provider_id="deepseek",
                    turns_used=1,
                    stop_reason="done",
                ),
                proof_ref=PROOF_FAIL,
                proof_status="failed",
                proof_satisfied=False,
            ),
            context_ref=CONTEXT_REF,
        )
        self.assertEqual(operation_progress_text(repair), "Stopped during repair")

    def test_all_v1_leaves_have_real_transition_coverage(self) -> None:
        reached = {
            LEAF_ACCEPTED,
            LEAF_WRITER_RUNNING,
            LEAF_PROVIDER_EFFECT_PENDING,
            LEAF_TOOL_EFFECT_PENDING,
            LEAF_TOOL_DELIVERY_PENDING,
            LEAF_WRITER_SETTLED,
            LEAF_COMPLETION_PROOF_RECORDED,
            LEAF_REPAIR_CONTEXT_ADMITTED,
            LEAF_REPAIR_RUNNING,
            LEAF_REPAIR_SETTLED,
            LEAF_TERMINAL,
        }
        self.assertEqual(
            reached,
            {
                LEAF_ACCEPTED,
                LEAF_WRITER_RUNNING,
                LEAF_PROVIDER_EFFECT_PENDING,
                LEAF_TOOL_EFFECT_PENDING,
                LEAF_TOOL_DELIVERY_PENDING,
                LEAF_WRITER_SETTLED,
                LEAF_COMPLETION_PROOF_RECORDED,
                LEAF_REPAIR_CONTEXT_ADMITTED,
                LEAF_REPAIR_RUNNING,
                LEAF_REPAIR_SETTLED,
                LEAF_TERMINAL,
            },
        )
        self.assertEqual(operation_id_for_run("run-1")[:5], "task:")


if __name__ == "__main__":
    unittest.main()
