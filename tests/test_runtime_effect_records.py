from __future__ import annotations

import tempfile
import unittest

from codey.runtime.effect_records import (
    EFFECT_CATEGORY_PROVIDER_SEND,
    EFFECT_CATEGORY_TOOL_CALL,
    RuntimeEffectError,
    RuntimeEffectIntent,
    RuntimeEffectSettlement,
    RuntimeEffectStore,
    SENT_STATE_MAYBE_SENT,
    SETTLEMENT_STATUS_ERROR,
    SETTLEMENT_STATUS_OK,
    compute_args_digest,
    new_effect_id,
)
from codey.runtime.mutation_line import RuntimeMutationLine
from codey.runtime.operation_state import (
    DRIVER_WRITER,
    LEAF_PROVIDER_EFFECT_PENDING,
    LEAF_TOOL_DELIVERY_PENDING,
    LEAF_WRITER_RUNNING,
    RuntimeOperationStore,
    mark_writer_running,
)
from codey.runtime.replay_policy import ReplayClass
from codey.runtime.session_log import RuntimeLogWriteError, RuntimeSessionLog


def _commit_log_entry(
    log: RuntimeSessionLog,
    session_id: str,
    *,
    lane: str,
    operation_id: str,
    kind: str,
    payload: dict[str, object],
) -> None:
    log.mutate(
        session_id,
        lambda _projection, _entries: (
            {
                "lane": lane,
                "operation_id": operation_id,
                "kind": kind,
                "payload": payload,
            },
        ),
    )


class RuntimeEffectRecordsTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.log = RuntimeSessionLog(self.temp_dir.name)
        self.line = RuntimeMutationLine(self.log)
        self.operations = RuntimeOperationStore(self.log)
        self.store = RuntimeEffectStore(self.log)
        self.session_id = "sess-1"
        self.run_id = "run-1"
        self.line.accept_operation(
            session_id=self.session_id,
            run_id=self.run_id,
            project="/tmp/test",
            provider_id="deepseek",
            turn_budget=10,
            max_repair_rounds=1,
            task_kind="project",
        )
        self.line.transition_operation(
            self.session_id,
            self.run_id,
            lambda state: mark_writer_running(state, provider_id="deepseek"),
        )

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_provider_intent_and_settlement_commit_with_state(self) -> None:
        effect_id = new_effect_id(EFFECT_CATEGORY_PROVIDER_SEND, self.run_id)
        intent = RuntimeEffectIntent(
            effect_id=effect_id,
            effect_category=EFFECT_CATEGORY_PROVIDER_SEND,
            session_id=self.session_id,
            run_id=self.run_id,
            phase=DRIVER_WRITER,
            provider_id="deepseek",
            turn=1,
            replay_class=ReplayClass.UNSAFE,
        )
        self.line.begin_provider_effect(
            self.session_id,
            self.run_id,
            intent,
            driver=DRIVER_WRITER,
        )
        pending = self.operations.load(self.session_id, self.run_id)
        assert pending is not None
        self.assertEqual(pending.leaf, LEAF_PROVIDER_EFFECT_PENDING)
        self.assertEqual(pending.pending_effect_ids, (effect_id,))

        self.line.settle_provider_effect(
            self.session_id,
            self.run_id,
            RuntimeEffectSettlement(
                effect_id=effect_id,
                effect_category=EFFECT_CATEGORY_PROVIDER_SEND,
                session_id=self.session_id,
                run_id=self.run_id,
                status=SETTLEMENT_STATUS_OK,
                sent_state="settled",
            ),
        )

        settled = self.operations.load(self.session_id, self.run_id)
        effects = self.store.load_effects(self.session_id, self.run_id)
        assert settled is not None
        self.assertEqual(settled.leaf, LEAF_WRITER_RUNNING)
        self.assertEqual(len(effects), 1)
        self.assertFalse(effects[0].is_pending)
        assert effects[0].settlement is not None
        self.assertEqual(effects[0].settlement.status, SETTLEMENT_STATUS_OK)

    def test_tool_intent_and_settlement_commit_with_delivery_pending(self) -> None:
        from codey.runtime.tool_result_delivery import (
            DeliveryBatchIntent,
            DeliveryBatchItem,
            compute_batch_digest,
        )

        effect_id = new_effect_id(EFFECT_CATEGORY_TOOL_CALL, self.run_id)
        intent = RuntimeEffectIntent(
            effect_id=effect_id,
            effect_category=EFFECT_CATEGORY_TOOL_CALL,
            session_id=self.session_id,
            run_id=self.run_id,
            phase=DRIVER_WRITER,
            tool_name="read",
            tool_id="1:0",
            turn=1,
            tool_index=0,
            replay_class=ReplayClass.SAFE,
            replay_args={"path": "foo.py"},
            args_digest=compute_args_digest({"path": "foo.py"}),
        )
        items = (
            DeliveryBatchItem(
                tool_index=0,
                tool_name="read",
                ref=effect_id,
                replay_class="safe",
            ),
        )
        self.line.begin_tool_batch(
            self.session_id,
            self.run_id,
            intents=(intent,),
            delivery_intent=DeliveryBatchIntent(
                batch_id="batch-1",
                session_id=self.session_id,
                run_id=self.run_id,
                turn=1,
                items=items,
                batch_digest=compute_batch_digest(items),
            ),
        )
        self.line.settle_tool_effect(
            self.session_id,
            self.run_id,
            RuntimeEffectSettlement(
                effect_id=effect_id,
                effect_category=EFFECT_CATEGORY_TOOL_CALL,
                session_id=self.session_id,
                run_id=self.run_id,
                status=SETTLEMENT_STATUS_OK,
                replay_class=ReplayClass.SAFE,
            ),
        )
        state = self.operations.load(self.session_id, self.run_id)
        effects = self.store.load_effects(self.session_id, self.run_id)
        assert state is not None
        self.assertEqual(state.leaf, LEAF_TOOL_DELIVERY_PENDING)
        self.assertFalse(effects[0].is_pending)

    def test_settlement_without_intent_raises(self) -> None:
        with self.assertRaises(RuntimeEffectError) as ctx:
            self.line.settle_tool_effect(
                self.session_id,
                self.run_id,
                RuntimeEffectSettlement(
                    effect_id="orphan",
                    effect_category=EFFECT_CATEGORY_TOOL_CALL,
                    session_id=self.session_id,
                    run_id=self.run_id,
                    status=SETTLEMENT_STATUS_OK,
                ),
            )
        self.assertIn("effect intent not found", str(ctx.exception))

    def test_repair_round_category_is_not_a_runtime_effect(self) -> None:
        with self.assertRaises(RuntimeEffectError):
            RuntimeEffectIntent(
                effect_id="eff-repair",
                effect_category="repair_round",
                session_id=self.session_id,
                run_id=self.run_id,
            )

    def test_replay_args_round_trip_and_validation(self) -> None:
        effect_id = new_effect_id(EFFECT_CATEGORY_TOOL_CALL, self.run_id)
        intent = RuntimeEffectIntent(
            effect_id=effect_id,
            effect_category=EFFECT_CATEGORY_TOOL_CALL,
            session_id=self.session_id,
            run_id=self.run_id,
            phase=DRIVER_WRITER,
            tool_name="read",
            turn=1,
            tool_index=0,
            replay_class=ReplayClass.SAFE,
            replay_args={"path": "foo/bar.py", "offset": 10},
        )
        from codey.runtime.tool_result_delivery import (
            DeliveryBatchIntent,
            DeliveryBatchItem,
            compute_batch_digest,
        )

        items = (
            DeliveryBatchItem(0, "read", effect_id, "safe"),
        )
        self.line.begin_tool_batch(
            self.session_id,
            self.run_id,
            intents=(intent,),
            delivery_intent=DeliveryBatchIntent(
                "batch-replay",
                self.session_id,
                self.run_id,
                1,
                items,
                compute_batch_digest(items),
            ),
        )
        loaded = self.store.load_effects(self.session_id, self.run_id)
        self.assertEqual(loaded[0].intent.replay_args, {"path": "foo/bar.py", "offset": 10})

        with self.assertRaises(RuntimeEffectError):
            RuntimeEffectIntent(
                effect_id="eff-unsafe-args",
                effect_category=EFFECT_CATEGORY_TOOL_CALL,
                session_id=self.session_id,
                run_id=self.run_id,
                tool_name="edit",
                replay_class=ReplayClass.UNSAFE,
                replay_args={"path": "foo.py"},
            )

    def test_projection_rejects_duplicate_and_orphan_records(self) -> None:
        from codey.runtime.operation_state import lane_for_run, operation_id_for_run

        lane = lane_for_run(self.run_id)
        op_id = operation_id_for_run(self.run_id)
        payload = {
            "schema_version": 1,
            "effect_kind": "runtime_effect",
            "record_kind": "settlement",
            "ref": "effect_settlement:eff-orphan",
            "effect_id": "eff-orphan",
            "effect_category": "tool_call",
            "session_id": self.session_id,
            "run_id": self.run_id,
            "lane": lane,
            "operation_id": op_id,
            "status": "ok",
            "sent_state": "settled",
            "replay_class": "unsafe",
        }
        _commit_log_entry(
            self.log,
            self.session_id,
            lane=lane,
            operation_id=op_id,
            kind="operation_effect",
            payload=payload,
        )
        with self.assertRaises(RuntimeEffectError):
            self.store.load_effects(self.session_id, self.run_id)

    def test_recovery_summary_counts_interrupted_and_replayed_effects(self) -> None:
        unsafe_id = new_effect_id(EFFECT_CATEGORY_TOOL_CALL, self.run_id)
        provider_id = new_effect_id(EFFECT_CATEGORY_PROVIDER_SEND, self.run_id)
        read_id = new_effect_id(EFFECT_CATEGORY_TOOL_CALL, self.run_id)

        self.line.begin_provider_effect(
            self.session_id,
            self.run_id,
            RuntimeEffectIntent(
                provider_id,
                EFFECT_CATEGORY_PROVIDER_SEND,
                self.session_id,
                self.run_id,
                phase=DRIVER_WRITER,
                provider_id="deepseek",
                turn=1,
            ),
        )
        self.line.settle_provider_effect(
            self.session_id,
            self.run_id,
            RuntimeEffectSettlement(
                provider_id,
                EFFECT_CATEGORY_PROVIDER_SEND,
                self.session_id,
                self.run_id,
                status="interrupted",
                sent_state=SENT_STATE_MAYBE_SENT,
            ),
        )

        from codey.runtime.tool_result_delivery import (
            DeliveryBatchIntent,
            DeliveryBatchItem,
            compute_batch_digest,
        )

        items = (
            DeliveryBatchItem(0, "edit", unsafe_id, "unsafe"),
            DeliveryBatchItem(1, "read", read_id, "safe"),
        )
        self.line.begin_tool_batch(
            self.session_id,
            self.run_id,
            intents=(
                RuntimeEffectIntent(
                    unsafe_id,
                    EFFECT_CATEGORY_TOOL_CALL,
                    self.session_id,
                    self.run_id,
                    phase=DRIVER_WRITER,
                    tool_name="edit",
                    turn=2,
                    replay_class=ReplayClass.UNSAFE,
                ),
                RuntimeEffectIntent(
                    read_id,
                    EFFECT_CATEGORY_TOOL_CALL,
                    self.session_id,
                    self.run_id,
                    phase=DRIVER_WRITER,
                    tool_name="read",
                    turn=2,
                    tool_index=1,
                    replay_class=ReplayClass.SAFE,
                    replay_args={"path": "foo.py"},
                ),
            ),
            delivery_intent=DeliveryBatchIntent(
                "batch-summary",
                self.session_id,
                self.run_id,
                2,
                items,
                compute_batch_digest(items),
            ),
        )
        self.line.settle_tool_effect(
            self.session_id,
            self.run_id,
            RuntimeEffectSettlement(
                unsafe_id,
                EFFECT_CATEGORY_TOOL_CALL,
                self.session_id,
                self.run_id,
                status=SETTLEMENT_STATUS_ERROR,
                replay_class=ReplayClass.UNSAFE,
            ),
        )
        self.line.settle_tool_effect(
            self.session_id,
            self.run_id,
            RuntimeEffectSettlement(
                read_id,
                EFFECT_CATEGORY_TOOL_CALL,
                self.session_id,
                self.run_id,
                status=SETTLEMENT_STATUS_OK,
                replay_class=ReplayClass.SAFE,
                replay_count=1,
                replayed_from_effect_id=read_id,
            ),
        )

        summary = self.store.recovery_summary(self.session_id, self.run_id)
        self.assertEqual(summary.unconfirmed_provider_calls, 1)
        self.assertEqual(summary.replayed_reads, 1)
        self.assertIn("Provider response was not confirmed", summary.explanation_lines)
        self.assertIn("Read action was recovered", summary.explanation_lines)

    def test_forbidden_payload_fields_rejected_by_log(self) -> None:
        with self.assertRaises(RuntimeLogWriteError):
            _commit_log_entry(
                self.log,
                self.session_id,
                lane="task:default",
                operation_id=f"run:{self.run_id}",
                kind="operation_effect",
                payload={"effect_kind": "runtime_effect", "raw_prompt": "hello secret"},
            )


if __name__ == "__main__":
    unittest.main()
