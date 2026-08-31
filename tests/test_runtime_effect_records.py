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
from codey.runtime.effects import RuntimeOperationStore, lane_for_run, operation_id_for_run
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
        self.lane = lane_for_run(self.run_id)
        self.op_id = operation_id_for_run(self.run_id)
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

    def test_deserialization_missing_required_semantic_fields_raises(self) -> None:
        base_intent_payload = {
            "schema_version": 1,
            "effect_kind": "runtime_effect",
            "record_kind": "intent",
            "ref": "effect:eff_1",
            "effect_id": "eff_1",
            "effect_category": "tool_call",
            "session_id": "s1",
            "run_id": "r1",
            "lane": self.lane,
            "operation_id": self.op_id,
            "turn": 1,
            "tool_index": 0,
            "replay_class": "safe",
        }
        # Missing replay_class
        p = dict(base_intent_payload)
        del p["replay_class"]
        with self.assertRaises(RuntimeEffectError):
            RuntimeEffectIntent.from_payload(p)

        # Unhashable list for replay_class raises RuntimeEffectError
        p = dict(base_intent_payload, replay_class=["safe"])
        with self.assertRaises(RuntimeEffectError):
            RuntimeEffectIntent.from_payload(p)

        # Missing effect_category
        p = dict(base_intent_payload)
        del p["effect_category"]
        with self.assertRaises(RuntimeEffectError):
            RuntimeEffectIntent.from_payload(p)

        # Missing turn
        p = dict(base_intent_payload)
        del p["turn"]
        with self.assertRaises(RuntimeEffectError):
            RuntimeEffectIntent.from_payload(p)

        # Missing tool_index
        p = dict(base_intent_payload)
        del p["tool_index"]
        with self.assertRaises(RuntimeEffectError):
            RuntimeEffectIntent.from_payload(p)

        # Missing lane
        p = dict(base_intent_payload)
        del p["lane"]
        with self.assertRaises(RuntimeEffectError):
            RuntimeEffectIntent.from_payload(p)

        base_settlement_payload = {
            "schema_version": 1,
            "effect_kind": "runtime_effect",
            "record_kind": "settlement",
            "ref": "effect_settlement:eff_1",
            "effect_id": "eff_1",
            "effect_category": "tool_call",
            "session_id": "s1",
            "run_id": "r1",
            "lane": self.lane,
            "operation_id": self.op_id,
            "status": "ok",
            "sent_state": "settled",
            "replay_class": "unsafe",
        }
        # Missing status
        p = dict(base_settlement_payload)
        del p["status"]
        with self.assertRaises(RuntimeEffectError):
            RuntimeEffectSettlement.from_payload(p)

        # Dict for status raises RuntimeEffectError
        p = dict(base_settlement_payload, status={"status": "ok"})
        with self.assertRaises(RuntimeEffectError):
            RuntimeEffectSettlement.from_payload(p)

        # Missing sent_state
        p = dict(base_settlement_payload)
        del p["sent_state"]
        with self.assertRaises(RuntimeEffectError):
            RuntimeEffectSettlement.from_payload(p)

        # Missing operation_id
        p = dict(base_settlement_payload)
        del p["operation_id"]
        with self.assertRaises(RuntimeEffectError):
            RuntimeEffectSettlement.from_payload(p)

        # Wrong refs and unknown keys are rejected instead of silently ignored.
        p = dict(base_intent_payload, ref="effect:other")
        with self.assertRaises(RuntimeEffectError):
            RuntimeEffectIntent.from_payload(p)

        p = dict(base_settlement_payload, ref="effect_settlement:other")
        with self.assertRaises(RuntimeEffectError):
            RuntimeEffectSettlement.from_payload(p)

        p = dict(base_intent_payload, unused_future_field=True)
        with self.assertRaises(RuntimeEffectError):
            RuntimeEffectIntent.from_payload(p)

        p = dict(base_settlement_payload, unused_future_field=True)
        with self.assertRaises(RuntimeEffectError):
            RuntimeEffectSettlement.from_payload(p)

        # Dataclass construction also keeps timestamp fields bounded.
        with self.assertRaises(RuntimeEffectError):
            RuntimeEffectIntent(
                effect_id="bad_created_at",
                effect_category=EFFECT_CATEGORY_TOOL_CALL,
                session_id=self.session_id,
                run_id=self.run_id,
                created_at="x" * 121,
            )

        with self.assertRaises(RuntimeEffectError):
            RuntimeEffectSettlement(
                effect_id="bad_created_at_settlement",
                effect_category=EFFECT_CATEGORY_TOOL_CALL,
                session_id=self.session_id,
                run_id=self.run_id,
                created_at="x" * 121,
            )

    def test_load_effects_mismatched_lane_or_operation_raises(self) -> None:
        # Start a second operation for run-2
        run_2 = "run-2"
        self.operations.start(
            session_id=self.session_id,
            run_id=run_2,
            project="/tmp/test",
            provider_id="deepseek",
            turn_budget=5,
            max_repair_rounds=1,
            task_kind="project",
        )
        # Append an entry under run-2's lane, but claiming to be for self.run_id
        lane_2 = lane_for_run(run_2)
        op_2 = operation_id_for_run(run_2)
        self.log.append(
            self.session_id,
            lane=lane_2,
            operation_id=op_2,
            kind="operation_effect",
            payload={
                "schema_version": 1,
                "effect_kind": "runtime_effect",
                "record_kind": "intent",
                "ref": "effect:eff_mismatched",
                "effect_id": "eff_mismatched",
                "effect_category": "tool_call",
                "session_id": self.session_id,
                "run_id": self.run_id,
                "lane": lane_2,
                "operation_id": op_2,
                "replay_class": "unsafe",
            },
        )
        with self.assertRaises(RuntimeEffectError) as ctx:
            self.store.load_effects(self.session_id, self.run_id)
        self.assertIn("does not match expected lane", str(ctx.exception))

    def test_direct_log_duplicate_intent_raises(self) -> None:
        eff_id = "eff_dup_intent"
        intent_payload = {
            "schema_version": 1,
            "effect_kind": "runtime_effect",
            "record_kind": "intent",
            "ref": f"effect:{eff_id}",
            "effect_id": eff_id,
            "effect_category": "tool_call",
            "session_id": self.session_id,
            "run_id": self.run_id,
            "lane": self.lane,
            "operation_id": self.op_id,
            "turn": 1,
            "tool_index": 0,
            "replay_class": "safe",
        }
        self.log.append(self.session_id, lane=self.lane, operation_id=self.op_id, kind="operation_effect", payload=intent_payload)
        self.log.append(self.session_id, lane=self.lane, operation_id=self.op_id, kind="operation_effect", payload=intent_payload)

        with self.assertRaises(RuntimeEffectError) as ctx:
            self.store.load_effects(self.session_id, self.run_id)
        self.assertIn("duplicate intent in session log", str(ctx.exception))

    def test_direct_log_orphan_settlement_raises(self) -> None:
        eff_id = "eff_orphan_settlement"
        settlement_payload = {
            "schema_version": 1,
            "effect_kind": "runtime_effect",
            "record_kind": "settlement",
            "ref": f"effect_settlement:{eff_id}",
            "effect_id": eff_id,
            "effect_category": "tool_call",
            "session_id": self.session_id,
            "run_id": self.run_id,
            "lane": self.lane,
            "operation_id": self.op_id,
            "status": "ok",
            "sent_state": "settled",
            "replay_class": "unsafe",
        }
        self.log.append(self.session_id, lane=self.lane, operation_id=self.op_id, kind="operation_effect", payload=settlement_payload)

        with self.assertRaises(RuntimeEffectError) as ctx:
            self.store.load_effects(self.session_id, self.run_id)
        self.assertIn("orphan settlement without intent in session log", str(ctx.exception))

    def test_direct_log_conflicting_settlement_raises(self) -> None:
        eff_id = "eff_conflict_settlement"
        intent_payload = {
            "schema_version": 1,
            "effect_kind": "runtime_effect",
            "record_kind": "intent",
            "ref": f"effect:{eff_id}",
            "effect_id": eff_id,
            "effect_category": "tool_call",
            "session_id": self.session_id,
            "run_id": self.run_id,
            "lane": self.lane,
            "operation_id": self.op_id,
            "turn": 1,
            "tool_index": 0,
            "replay_class": "unsafe",
        }
        settlement1 = {
            "schema_version": 1,
            "effect_kind": "runtime_effect",
            "record_kind": "settlement",
            "ref": f"effect_settlement:{eff_id}",
            "effect_id": eff_id,
            "effect_category": "tool_call",
            "session_id": self.session_id,
            "run_id": self.run_id,
            "lane": self.lane,
            "operation_id": self.op_id,
            "status": "ok",
            "sent_state": "settled",
            "replay_class": "unsafe",
        }
        settlement2 = dict(settlement1, status="error", error_code="disk_full")

        self.log.append(self.session_id, lane=self.lane, operation_id=self.op_id, kind="operation_effect", payload=intent_payload)
        self.log.append(self.session_id, lane=self.lane, operation_id=self.op_id, kind="operation_effect", payload=settlement1)
        self.log.append(self.session_id, lane=self.lane, operation_id=self.op_id, kind="operation_effect", payload=settlement2)

        with self.assertRaises(RuntimeEffectError) as ctx:
            self.store.load_effects(self.session_id, self.run_id)
        self.assertIn("conflicting duplicate settlement in session log", str(ctx.exception))

    def test_direct_log_mismatched_session_id_raises(self) -> None:
        eff_id = "eff_mismatched_session"
        intent_payload = {
            "schema_version": 1,
            "effect_kind": "runtime_effect",
            "record_kind": "intent",
            "ref": f"effect:{eff_id}",
            "effect_id": eff_id,
            "effect_category": "tool_call",
            "session_id": "other_session_id",
            "run_id": self.run_id,
            "lane": self.lane,
            "operation_id": self.op_id,
            "turn": 1,
            "tool_index": 0,
            "replay_class": "safe",
        }
        self.log.append(self.session_id, lane=self.lane, operation_id=self.op_id, kind="operation_effect", payload=intent_payload)

        with self.assertRaises(RuntimeEffectError) as ctx:
            self.store.load_effects(self.session_id, self.run_id)
        self.assertIn("payload session_id 'other_session_id' does not match expected session_id", str(ctx.exception))

    def test_direct_log_missing_or_wrong_run_id_in_current_lane_raises(self) -> None:
        eff_id = "eff_wrong_run"
        intent_payload_wrong = {
            "schema_version": 1,
            "effect_kind": "runtime_effect",
            "record_kind": "intent",
            "ref": f"effect:{eff_id}",
            "effect_id": eff_id,
            "effect_category": "tool_call",
            "session_id": self.session_id,
            "run_id": "wrong_run_id",
            "lane": self.lane,
            "operation_id": self.op_id,
            "turn": 1,
            "tool_index": 0,
            "replay_class": "safe",
        }
        self.log.append(self.session_id, lane=self.lane, operation_id=self.op_id, kind="operation_effect", payload=intent_payload_wrong)

        with self.assertRaises(RuntimeEffectError) as ctx:
            self.store.load_effects(self.session_id, self.run_id)
        self.assertIn("payload run_id 'wrong_run_id' does not match expected run_id", str(ctx.exception))

    def test_record_intent_and_settlement_mismatched_boundary_raises(self) -> None:
        valid_intent = RuntimeEffectIntent(
            effect_id="eff_boundary_test",
            effect_category="tool_call",
            session_id=self.session_id,
            run_id=self.run_id,
            lane=self.lane,
            operation_id=self.op_id,
            turn=1,
            tool_index=0,
            replay_class="safe",
        )
        # Mismatched session_id in record_intent
        with self.assertRaises(RuntimeEffectError) as ctx:
            self.store.record_intent("other_session", self.run_id, valid_intent)
        self.assertIn("does not match expected session_id", str(ctx.exception))

        # Mismatched run_id in record_intent
        with self.assertRaises(RuntimeEffectError) as ctx:
            self.store.record_intent(self.session_id, "other_run", valid_intent)
        self.assertIn("does not match expected run_id", str(ctx.exception))

        # Valid record_intent succeeds
        self.store.record_intent(self.session_id, self.run_id, valid_intent)

        valid_settlement = RuntimeEffectSettlement(
            effect_id="eff_boundary_test",
            effect_category="tool_call",
            session_id=self.session_id,
            run_id=self.run_id,
            lane=self.lane,
            operation_id=self.op_id,
            status="ok",
            sent_state="settled",
            replay_class="safe",
        )
        # Mismatched session_id in record_settlement
        with self.assertRaises(RuntimeEffectError) as ctx:
            self.store.record_settlement("other_session", self.run_id, valid_settlement)
        self.assertIn("does not match expected session_id", str(ctx.exception))

        # Mismatched run_id in record_settlement
        with self.assertRaises(RuntimeEffectError) as ctx:
            self.store.record_settlement(self.session_id, "other_run", valid_settlement)
        self.assertIn("does not match expected run_id", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
