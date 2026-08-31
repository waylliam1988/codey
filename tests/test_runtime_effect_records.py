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
    SETTLEMENT_STATUS_OK,
    compute_args_digest,
)
from codey.runtime.replay_policy import ReplayClass
from codey.runtime.session_log import RuntimeLogWriteError, RuntimeSessionLog


class RuntimeEffectRecordsTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.log = RuntimeSessionLog(self.temp_dir.name)
        self.store = RuntimeEffectStore(self.log)
        self.session_id = "sess-1"
        self.run_id = "run-1"
        # Start the task operation in the session log
        from codey.storage.local_store import session_key
        self.op_id = f"task:{session_key(self.run_id)}"
        self.log.append(
            self.session_id,
            lane="task",
            operation_id=self.op_id,
            kind="operation_started",
            payload={"operation_kind": "task"},
        )

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_record_intent_and_settlement_round_trip(self) -> None:
        intent = RuntimeEffectIntent(
            effect_id="tool_1",
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
        self.assertEqual(effects[0].intent.effect_id, "tool_1")
        self.assertTrue(effects[0].is_pending)
        self.assertFalse(effects[0].is_settled)

        settlement = RuntimeEffectSettlement(
            effect_id="tool_1",
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

    def test_pending_effects_projection(self) -> None:
        # Tool 1: settled
        self.store.record_intent(
            self.session_id,
            self.run_id,
            RuntimeEffectIntent(
                effect_id="tool_1",
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
                effect_id="tool_1",
                effect_category=EFFECT_CATEGORY_TOOL_CALL,
                session_id=self.session_id,
                run_id=self.run_id,
                status=SETTLEMENT_STATUS_OK,
                replay_class=ReplayClass.SAFE,
            ),
        )

        # Tool 2: pending (interrupted before settlement)
        self.store.record_intent(
            self.session_id,
            self.run_id,
            RuntimeEffectIntent(
                effect_id="tool_2",
                effect_category=EFFECT_CATEGORY_TOOL_CALL,
                session_id=self.session_id,
                run_id=self.run_id,
                tool_name="edit",
                replay_class=ReplayClass.UNSAFE,
            ),
        )

        pending = self.store.pending_effects(self.session_id, self.run_id)
        self.assertEqual(len(pending), 1)
        self.assertEqual(pending[0].intent.effect_id, "tool_2")

    def test_synthesize_interrupted_settlement(self) -> None:
        self.store.record_intent(
            self.session_id,
            self.run_id,
            RuntimeEffectIntent(
                effect_id="tool_3",
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
            "tool_3",
            reason="interrupted_by_crash",
        )
        self.assertEqual(synthesized.status, "interrupted")
        self.assertEqual(synthesized.error_code, "interrupted_by_crash")

        # Now pending should be empty
        self.assertEqual(len(self.store.pending_effects(self.session_id, self.run_id)), 0)

    def test_recovery_summary_explanations(self) -> None:
        # 1 unsafe tool interrupted
        self.store.record_intent(
            self.session_id,
            self.run_id,
            RuntimeEffectIntent(
                effect_id="tool_edit",
                effect_category=EFFECT_CATEGORY_TOOL_CALL,
                session_id=self.session_id,
                run_id=self.run_id,
                tool_name="edit",
                replay_class=ReplayClass.UNSAFE,
            ),
        )
        self.store.synthesize_interrupted(self.session_id, self.run_id, "tool_edit")

        # 1 safe tool interrupted
        self.store.record_intent(
            self.session_id,
            self.run_id,
            RuntimeEffectIntent(
                effect_id="tool_read",
                effect_category=EFFECT_CATEGORY_TOOL_CALL,
                session_id=self.session_id,
                run_id=self.run_id,
                tool_name="read",
                replay_class=ReplayClass.SAFE,
            ),
        )
        self.store.synthesize_interrupted(self.session_id, self.run_id, "tool_read")

        # 1 provider send interrupted
        self.store.record_intent(
            self.session_id,
            self.run_id,
            RuntimeEffectIntent(
                effect_id="psend_1",
                effect_category=EFFECT_CATEGORY_PROVIDER_SEND,
                session_id=self.session_id,
                run_id=self.run_id,
                provider_id="deepseek",
            ),
        )
        self.store.synthesize_interrupted(self.session_id, self.run_id, "psend_1")

        summary = self.store.recovery_summary(self.session_id, self.run_id)
        self.assertEqual(summary.interrupted_writes, 1)
        self.assertEqual(summary.retryable_reads, 1)
        self.assertEqual(summary.unconfirmed_provider_calls, 1)
        self.assertIn("Local write was interrupted and was not repeated", summary.explanation_lines)
        self.assertIn("Read action can be retried", summary.explanation_lines)
        self.assertIn("Provider response was not confirmed", summary.explanation_lines)

    def test_forbidden_payload_fields_rejected_by_log(self) -> None:
        with self.assertRaises(RuntimeLogWriteError):
            self.log.append(
                self.session_id,
                lane="task",
                operation_id="task:1",
                kind="operation_effect",
                payload={"effect_kind": "runtime_effect", "raw_prompt": "hello secret"},
            )

    def test_unknown_category_raises(self) -> None:
        with self.assertRaises(RuntimeEffectError):
            RuntimeEffectIntent(
                effect_id="bad_1",
                effect_category="unknown_category",
                session_id=self.session_id,
                run_id=self.run_id,
            )


if __name__ == "__main__":
    unittest.main()
