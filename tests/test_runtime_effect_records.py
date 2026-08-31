"""Tests for runtime effect records, intent/settlement persistence, and recovery projection."""

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
    record_settlement_safely,
)
from codey.runtime.effects import RuntimeOperationStore
from codey.runtime.replay_policy import ReplayClass
from codey.runtime.session_log import RuntimeLogWriteError, RuntimeSessionLog


class RuntimeEffectRecordsTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.log = RuntimeSessionLog(self.temp_dir.name)
        self.operations = RuntimeOperationStore(self.log)
        self.store = RuntimeEffectStore(self.log)
        self.session_id = "sess-1"
        self.run_id = "run-1"
        # Start a real operation via RuntimeOperationStore
        self.operations.start(
            session_id=self.session_id,
            run_id=self.run_id,
            project="/tmp/test",
            provider_id="deepseek",
            turn_budget=10,
            max_repair_rounds=1,
            task_kind="project",
        )

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_record_intent_and_settlement_round_trip(self) -> None:
        effect_id = new_effect_id(EFFECT_CATEGORY_TOOL_CALL, self.run_id)
        intent = RuntimeEffectIntent(
            effect_id=effect_id,
            effect_category=EFFECT_CATEGORY_TOOL_CALL,
            session_id=self.session_id,
            run_id=self.run_id,
            tool_name="edit",
            turn=1,
            replay_class=ReplayClass.UNSAFE,
            args_digest=compute_args_digest({"path": "foo.py"}),
        )
        self.store.record_intent(self.session_id, self.run_id, intent)

        effects = self.store.load_effects(self.session_id, self.run_id)
        self.assertEqual(len(effects), 1)
        self.assertEqual(effects[0].intent.effect_id, effect_id)
        self.assertTrue(effects[0].is_pending)
        self.assertFalse(effects[0].is_settled)

        settlement = RuntimeEffectSettlement(
            effect_id=effect_id,
            effect_category=EFFECT_CATEGORY_TOOL_CALL,
            session_id=self.session_id,
            run_id=self.run_id,
            status=SETTLEMENT_STATUS_OK,
            replay_class=ReplayClass.UNSAFE,
        )
        self.store.record_settlement(self.session_id, self.run_id, settlement)

        effects_after = self.store.load_effects(self.session_id, self.run_id)
        self.assertEqual(len(effects_after), 1)
        self.assertFalse(effects_after[0].is_pending)
        self.assertTrue(effects_after[0].is_settled)
        self.assertEqual(effects_after[0].settlement.status, SETTLEMENT_STATUS_OK)

    def test_record_settlement_without_intent_raises(self) -> None:
        orphan = RuntimeEffectSettlement(
            effect_id="orphan_effect_id",
            effect_category=EFFECT_CATEGORY_TOOL_CALL,
            session_id=self.session_id,
            run_id=self.run_id,
            status=SETTLEMENT_STATUS_OK,
        )
        with self.assertRaises(RuntimeEffectError) as ctx:
            self.store.record_settlement(self.session_id, self.run_id, orphan)
        self.assertIn("unknown intent", str(ctx.exception))

    def test_record_settlement_mismatched_category_raises(self) -> None:
        effect_id = new_effect_id(EFFECT_CATEGORY_TOOL_CALL, self.run_id)
        self.store.record_intent(
            self.session_id,
            self.run_id,
            RuntimeEffectIntent(
                effect_id=effect_id,
                effect_category=EFFECT_CATEGORY_TOOL_CALL,
                session_id=self.session_id,
                run_id=self.run_id,
                tool_name="edit",
            ),
        )
        # Attempt to settle tool_call intent with provider_send category
        mismatched = RuntimeEffectSettlement(
            effect_id=effect_id,
            effect_category=EFFECT_CATEGORY_PROVIDER_SEND,
            session_id=self.session_id,
            run_id=self.run_id,
            status=SETTLEMENT_STATUS_OK,
        )
        with self.assertRaises(RuntimeEffectError) as ctx:
            self.store.record_settlement(self.session_id, self.run_id, mismatched)
        self.assertIn("does not match intent category", str(ctx.exception))

    def test_record_settlement_duplicate_idempotent_and_conflict(self) -> None:
        effect_id = new_effect_id(EFFECT_CATEGORY_TOOL_CALL, self.run_id)
        self.store.record_intent(
            self.session_id,
            self.run_id,
            RuntimeEffectIntent(
                effect_id=effect_id,
                effect_category=EFFECT_CATEGORY_TOOL_CALL,
                session_id=self.session_id,
                run_id=self.run_id,
                tool_name="edit",
            ),
        )
        settlement1 = RuntimeEffectSettlement(
            effect_id=effect_id,
            effect_category=EFFECT_CATEGORY_TOOL_CALL,
            session_id=self.session_id,
            run_id=self.run_id,
            status=SETTLEMENT_STATUS_OK,
        )
        s1 = self.store.record_settlement(self.session_id, self.run_id, settlement1)

        # Identical settlement returns existing
        s2 = self.store.record_settlement(self.session_id, self.run_id, settlement1)
        self.assertEqual(s1.effect_id, s2.effect_id)

        # Conflicting settlement raises
        conflicting = RuntimeEffectSettlement(
            effect_id=effect_id,
            effect_category=EFFECT_CATEGORY_TOOL_CALL,
            session_id=self.session_id,
            run_id=self.run_id,
            status=SETTLEMENT_STATUS_ERROR,
            error_code="other_error",
        )
        with self.assertRaises(RuntimeEffectError) as ctx:
            self.store.record_settlement(self.session_id, self.run_id, conflicting)
        self.assertIn("already settled", str(ctx.exception))

    def test_pending_effects_projection(self) -> None:
        eff1 = new_effect_id(EFFECT_CATEGORY_TOOL_CALL, self.run_id)
        # Tool 1: settled
        self.store.record_intent(
            self.session_id,
            self.run_id,
            RuntimeEffectIntent(
                effect_id=eff1,
                effect_category=EFFECT_CATEGORY_TOOL_CALL,
                session_id=self.session_id,
                run_id=self.run_id,
                tool_name="read",
                replay_class=ReplayClass.SAFE,
            ),
        )
        self.store.record_settlement(
            self.session_id,
            self.run_id,
            RuntimeEffectSettlement(
                effect_id=eff1,
                effect_category=EFFECT_CATEGORY_TOOL_CALL,
                session_id=self.session_id,
                run_id=self.run_id,
                status=SETTLEMENT_STATUS_OK,
                replay_class=ReplayClass.SAFE,
            ),
        )

        # Tool 2: pending (interrupted before settlement)
        eff2 = new_effect_id(EFFECT_CATEGORY_TOOL_CALL, self.run_id)
        self.store.record_intent(
            self.session_id,
            self.run_id,
            RuntimeEffectIntent(
                effect_id=eff2,
                effect_category=EFFECT_CATEGORY_TOOL_CALL,
                session_id=self.session_id,
                run_id=self.run_id,
                tool_name="edit",
                replay_class=ReplayClass.UNSAFE,
            ),
        )

        pending = self.store.pending_effects(self.session_id, self.run_id)
        self.assertEqual(len(pending), 1)
        self.assertEqual(pending[0].intent.effect_id, eff2)

    def test_synthesize_interrupted_settlement(self) -> None:
        eff3 = new_effect_id(EFFECT_CATEGORY_TOOL_CALL, self.run_id)
        self.store.record_intent(
            self.session_id,
            self.run_id,
            RuntimeEffectIntent(
                effect_id=eff3,
                effect_category=EFFECT_CATEGORY_TOOL_CALL,
                session_id=self.session_id,
                run_id=self.run_id,
                tool_name="run",
                replay_class=ReplayClass.UNSAFE,
            ),
        )
        synthesized = self.store.synthesize_interrupted(
            self.session_id,
            self.run_id,
            eff3,
            reason="interrupted_by_crash",
        )
        self.assertEqual(synthesized.status, "interrupted")
        self.assertEqual(synthesized.error_code, "interrupted_by_crash")

        # Now pending should be empty
        self.assertEqual(len(self.store.pending_effects(self.session_id, self.run_id)), 0)

    def test_recovery_summary_ignores_in_flight_pending_and_normal_provider_error(self) -> None:
        # 1. In-flight pending effect
        in_flight_id = new_effect_id(EFFECT_CATEGORY_TOOL_CALL, self.run_id)
        self.store.record_intent(
            self.session_id,
            self.run_id,
            RuntimeEffectIntent(
                effect_id=in_flight_id,
                effect_category=EFFECT_CATEGORY_TOOL_CALL,
                session_id=self.session_id,
                run_id=self.run_id,
                tool_name="write",
                replay_class=ReplayClass.UNSAFE,
            ),
        )

        # 2. Normal provider error (status=error, maybe_sent)
        prov_id = new_effect_id(EFFECT_CATEGORY_PROVIDER_SEND, self.run_id)
        self.store.record_intent(
            self.session_id,
            self.run_id,
            RuntimeEffectIntent(
                effect_id=prov_id,
                effect_category=EFFECT_CATEGORY_PROVIDER_SEND,
                session_id=self.session_id,
                run_id=self.run_id,
                provider_id="deepseek",
            ),
        )
        self.store.record_settlement(
            self.session_id,
            self.run_id,
            RuntimeEffectSettlement(
                effect_id=prov_id,
                effect_category=EFFECT_CATEGORY_PROVIDER_SEND,
                session_id=self.session_id,
                run_id=self.run_id,
                status=SETTLEMENT_STATUS_ERROR,
                sent_state=SENT_STATE_MAYBE_SENT,
            ),
        )

        # Neither should show recovery explanations
        summary_in_flight = self.store.recovery_summary(self.session_id, self.run_id)
        self.assertEqual(summary_in_flight.explanation_lines, ())
        self.assertEqual(summary_in_flight.interrupted_writes, 0)
        self.assertEqual(summary_in_flight.unconfirmed_provider_calls, 0)

        # After synthetic crash recovery settlement, it should now explain
        self.store.synthesize_interrupted(self.session_id, self.run_id, in_flight_id)
        summary_after = self.store.recovery_summary(self.session_id, self.run_id)
        self.assertEqual(summary_after.interrupted_writes, 1)
        self.assertIn("Local write was interrupted and was not repeated", summary_after.explanation_lines)

    def test_unique_effect_id_prevents_resume_collision(self) -> None:
        eff_turn1_attempt1 = new_effect_id(EFFECT_CATEGORY_TOOL_CALL, self.run_id)
        self.store.record_intent(
            self.session_id,
            self.run_id,
            RuntimeEffectIntent(
                effect_id=eff_turn1_attempt1,
                effect_category=EFFECT_CATEGORY_TOOL_CALL,
                session_id=self.session_id,
                run_id=self.run_id,
                turn=1,
                tool_index=0,
                tool_name="edit",
            ),
        )
        self.store.record_settlement(
            self.session_id,
            self.run_id,
            RuntimeEffectSettlement(
                effect_id=eff_turn1_attempt1,
                effect_category=EFFECT_CATEGORY_TOOL_CALL,
                session_id=self.session_id,
                run_id=self.run_id,
                status=SETTLEMENT_STATUS_OK,
            ),
        )

        # Resume / failover
        eff_turn1_attempt2 = new_effect_id(EFFECT_CATEGORY_TOOL_CALL, self.run_id)
        self.assertNotEqual(eff_turn1_attempt1, eff_turn1_attempt2)

        self.store.record_intent(
            self.session_id,
            self.run_id,
            RuntimeEffectIntent(
                effect_id=eff_turn1_attempt2,
                effect_category=EFFECT_CATEGORY_TOOL_CALL,
                session_id=self.session_id,
                run_id=self.run_id,
                turn=1,
                tool_index=0,
                tool_name="edit",
            ),
        )

        pending = self.store.pending_effects(self.session_id, self.run_id)
        self.assertEqual(len(pending), 1)
        self.assertEqual(pending[0].intent.effect_id, eff_turn1_attempt2)

    def test_record_settlement_safely_never_raises(self) -> None:
        orphan = RuntimeEffectSettlement(
            effect_id="non_existent",
            effect_category=EFFECT_CATEGORY_TOOL_CALL,
            session_id=self.session_id,
            run_id=self.run_id,
        )
        result = record_settlement_safely(self.store, self.session_id, self.run_id, orphan)
        self.assertIsNone(result)

    def test_forbidden_payload_fields_rejected_by_log(self) -> None:
        with self.assertRaises(RuntimeLogWriteError):
            self.log.append(
                self.session_id,
                lane="task:default",
                operation_id=f"run:{self.run_id}",
                kind="operation_effect",
                payload={"effect_kind": "runtime_effect", "raw_prompt": "hello secret"},
            )

    def test_turn_tool_index_and_schema_validation(self) -> None:
        with self.assertRaises(RuntimeEffectError):
            RuntimeEffectIntent(
                effect_id="bad_turn_bool",
                effect_category=EFFECT_CATEGORY_TOOL_CALL,
                session_id=self.session_id,
                run_id=self.run_id,
                turn=True,  # bool is forbidden
            )
        with self.assertRaises(RuntimeEffectError):
            RuntimeEffectIntent(
                effect_id="bad_turn_neg",
                effect_category=EFFECT_CATEGORY_TOOL_CALL,
                session_id=self.session_id,
                run_id=self.run_id,
                turn=-1,
            )
        with self.assertRaises(RuntimeEffectError):
            RuntimeEffectIntent(
                effect_id="bad_schema",
                effect_category=EFFECT_CATEGORY_TOOL_CALL,
                session_id=self.session_id,
                run_id=self.run_id,
                schema_version=2,
            )


if __name__ == "__main__":
    unittest.main()
