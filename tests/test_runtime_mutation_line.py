from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from codey.runtime.effect_records import (
    EFFECT_CATEGORY_PROVIDER_SEND,
    EFFECT_CATEGORY_TOOL_CALL,
    RuntimeEffectIntent,
    RuntimeEffectSettlement,
    RuntimeEffectStore,
    SETTLEMENT_STATUS_OK,
    new_effect_id,
)
from codey.runtime.mutation_line import RuntimeMutationLine
from codey.runtime.operation_state import (
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
    RuntimeOperationStore,
    RuntimeOperationTransitionError,
)
from codey.runtime.replay_policy import ReplayClass
from codey.runtime.session_log import RuntimeSessionLog
from codey.runtime.tool_result_delivery import (
    DeliveryBatchIntent,
    DeliveryBatchItem,
    ToolResultDeliveryStore,
    compute_batch_digest,
    new_batch_id,
)


class RuntimeMutationLineTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.log = RuntimeSessionLog(Path(self.temp_dir.name))
        self.line = RuntimeMutationLine(self.log)
        self.operations = RuntimeOperationStore(self.log)
        self.effects = RuntimeEffectStore(self.log)
        self.delivery = ToolResultDeliveryStore(self.log)
        self.session_id = "sess-mutation-line"
        self.run_id = "run-mutation-line"

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def _accept_and_run_writer(self) -> None:
        self.line.accept_operation(
            session_id=self.session_id,
            run_id=self.run_id,
            project="",
            provider_id="mock",
            turn_budget=5,
            max_repair_rounds=1,
            task_kind="project",
        )
        self.line.mark_writer_running(
            self.session_id,
            self.run_id,
            provider_id="mock",
        )

    def _tool_intent(self, tool_name: str, tool_index: int) -> RuntimeEffectIntent:
        effect_id = new_effect_id(EFFECT_CATEGORY_TOOL_CALL, self.run_id)
        replay_args = {"path": "target.txt"} if tool_name == "read" else None
        return RuntimeEffectIntent(
            effect_id=effect_id,
            effect_category=EFFECT_CATEGORY_TOOL_CALL,
            session_id=self.session_id,
            run_id=self.run_id,
            phase="writer",
            turn=1,
            tool_index=tool_index,
            tool_name=tool_name,
            replay_class=ReplayClass.SAFE if replay_args is not None else ReplayClass.UNSAFE,
            replay_args=replay_args,
        )

    def test_accept_operation_commits_started_and_state_in_one_batch(self) -> None:
        state = self.line.accept_operation(
            session_id=self.session_id,
            run_id=self.run_id,
            project="",
            provider_id="mock",
            turn_budget=5,
            max_repair_rounds=1,
            task_kind="project",
        )

        self.assertIsNotNone(state)
        entries = self.log.entries(self.session_id)
        self.assertEqual([entry.kind for entry in entries], ["operation_started", "operation_state"])
        self.assertEqual({entry.batch_id for entry in entries}, {entries[0].batch_id})
        self.assertEqual(self.operations.load(self.session_id, self.run_id).leaf, LEAF_ACCEPTED)
        projection = self.log.projection(self.session_id)
        self.assertEqual(projection.operations[state.operation_id].status, "open")

    def test_begin_tool_batch_commits_effects_delivery_and_pending_state(self) -> None:
        self._accept_and_run_writer()
        read = self._tool_intent("read", 0)
        edit = self._tool_intent("edit", 1)
        items = (
            DeliveryBatchItem(0, "read", read.effect_id, "safe", False),
            DeliveryBatchItem(1, "edit", edit.effect_id, "unsafe", False),
        )
        before = len(self.log.entries(self.session_id))

        self.line.begin_tool_batch(
            self.session_id,
            self.run_id,
            intents=(read, edit),
            delivery_intent=DeliveryBatchIntent(
                batch_id=new_batch_id(self.run_id, 1),
                session_id=self.session_id,
                run_id=self.run_id,
                turn=1,
                items=items,
                batch_digest=compute_batch_digest(items),
            ),
        )

        rows = self.log.entries(self.session_id)[before:]
        self.assertEqual(
            [row.kind for row in rows],
            ["operation_effect", "operation_effect", "operation_effect", "operation_state"],
        )
        self.assertEqual({row.batch_id for row in rows}, {rows[0].batch_id})
        state = self.operations.load(self.session_id, self.run_id)
        self.assertEqual(state.leaf, LEAF_TOOL_EFFECT_PENDING)
        self.assertEqual(state.pending_effect_ids, (read.effect_id, edit.effect_id))
        self.assertEqual(len(self.delivery.load_batches(self.session_id, self.run_id)), 1)

    def test_settling_tool_effects_advances_to_delivery_pending(self) -> None:
        self._accept_and_run_writer()
        read = self._tool_intent("read", 0)
        items = (DeliveryBatchItem(0, "read", read.effect_id, "safe", False),)
        self.line.begin_tool_batch(
            self.session_id,
            self.run_id,
            intents=(read,),
            delivery_intent=DeliveryBatchIntent(
                batch_id=new_batch_id(self.run_id, 1),
                session_id=self.session_id,
                run_id=self.run_id,
                turn=1,
                items=items,
                batch_digest=compute_batch_digest(items),
            ),
        )
        before = len(self.log.entries(self.session_id))

        self.line.settle_tool_effect(
            self.session_id,
            self.run_id,
            RuntimeEffectSettlement(
                effect_id=read.effect_id,
                effect_category=EFFECT_CATEGORY_TOOL_CALL,
                session_id=self.session_id,
                run_id=self.run_id,
                status=SETTLEMENT_STATUS_OK,
                sent_state="settled",
                replay_class=ReplayClass.SAFE,
            ),
        )

        rows = self.log.entries(self.session_id)[before:]
        self.assertEqual([row.kind for row in rows], ["operation_effect", "operation_state"])
        self.assertEqual({row.batch_id for row in rows}, {rows[0].batch_id})
        self.assertEqual(self.operations.load(self.session_id, self.run_id).leaf, LEAF_TOOL_DELIVERY_PENDING)

    def test_record_delivery_recovered_requires_matching_delivery_pending_state(self) -> None:
        self._accept_and_run_writer()

        with self.assertRaises(RuntimeOperationTransitionError):
            self.line.record_delivery_recovered(
                self.session_id,
                self.run_id,
                batch_id="batch-not-pending",
                recovered_effect_ids=("eff-read",),
                recovered_reads=1,
            )

        read = self._tool_intent("read", 0)
        batch_id = new_batch_id(self.run_id, 1)
        items = (DeliveryBatchItem(0, "read", read.effect_id, "safe", False),)
        self.line.begin_tool_batch(
            self.session_id,
            self.run_id,
            intents=(read,),
            delivery_intent=DeliveryBatchIntent(
                batch_id=batch_id,
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
                effect_id=read.effect_id,
                effect_category=EFFECT_CATEGORY_TOOL_CALL,
                session_id=self.session_id,
                run_id=self.run_id,
                status=SETTLEMENT_STATUS_OK,
                sent_state="settled",
                replay_class=ReplayClass.SAFE,
            ),
        )

        with self.assertRaises(RuntimeOperationTransitionError):
            self.line.record_delivery_recovered(
                self.session_id,
                self.run_id,
                batch_id="batch-other",
                recovered_effect_ids=(read.effect_id,),
                recovered_reads=1,
            )

    def test_provider_delivery_receipt_commits_with_provider_settlement(self) -> None:
        self._accept_and_run_writer()
        read = self._tool_intent("read", 0)
        batch_id = new_batch_id(self.run_id, 1)
        items = (DeliveryBatchItem(0, "read", read.effect_id, "safe", False),)
        self.line.begin_tool_batch(
            self.session_id,
            self.run_id,
            intents=(read,),
            delivery_intent=DeliveryBatchIntent(
                batch_id=batch_id,
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
                effect_id=read.effect_id,
                effect_category=EFFECT_CATEGORY_TOOL_CALL,
                session_id=self.session_id,
                run_id=self.run_id,
                replay_class=ReplayClass.SAFE,
            ),
        )
        provider_effect_id = new_effect_id(EFFECT_CATEGORY_PROVIDER_SEND, self.run_id)
        self.line.begin_provider_effect(
            self.session_id,
            self.run_id,
            RuntimeEffectIntent(
                effect_id=provider_effect_id,
                effect_category=EFFECT_CATEGORY_PROVIDER_SEND,
                session_id=self.session_id,
                run_id=self.run_id,
                phase="writer",
                provider_id="mock",
                turn=1,
                replay_class=ReplayClass.UNSAFE,
            ),
            delivery_batch_id=batch_id,
        )
        self.assertEqual(self.operations.load(self.session_id, self.run_id).leaf, LEAF_PROVIDER_EFFECT_PENDING)
        before = len(self.log.entries(self.session_id))

        self.line.settle_provider_effect(
            self.session_id,
            self.run_id,
            RuntimeEffectSettlement(
                effect_id=provider_effect_id,
                effect_category=EFFECT_CATEGORY_PROVIDER_SEND,
                session_id=self.session_id,
                run_id=self.run_id,
                status=SETTLEMENT_STATUS_OK,
                sent_state="settled",
                replay_class=ReplayClass.UNSAFE,
            ),
        )

        rows = self.log.entries(self.session_id)[before:]
        self.assertEqual([row.kind for row in rows], ["operation_effect", "operation_effect", "operation_state"])
        self.assertEqual({row.batch_id for row in rows}, {rows[0].batch_id})
        self.assertEqual(self.operations.load(self.session_id, self.run_id).leaf, LEAF_WRITER_RUNNING)
        self.assertTrue(self.delivery.load_batches(self.session_id, self.run_id)[0].is_delivered)

    def test_mark_terminal_commits_state_and_operation_settlement_together(self) -> None:
        self._accept_and_run_writer()
        before = len(self.log.entries(self.session_id))

        self.line.mark_terminal(
            self.session_id,
            self.run_id,
            stop_reason="done",
            summary_chars=4,
            turns=1,
            max_turns=5,
            provider="mock",
        )

        rows = self.log.entries(self.session_id)[before:]
        self.assertEqual([row.kind for row in rows], ["operation_state", "operation_settled"])
        self.assertEqual({row.batch_id for row in rows}, {rows[0].batch_id})
        self.assertEqual(self.operations.load(self.session_id, self.run_id).leaf, LEAF_TERMINAL)
        projection = self.log.projection(self.session_id)
        operation_id = self.operations.load(self.session_id, self.run_id).operation_id
        self.assertEqual(projection.operations[operation_id].status, "settled")

    def test_terminal_recommit_must_match_existing_terminal_identity(self) -> None:
        self._accept_and_run_writer()
        self.line.mark_terminal(
            self.session_id,
            self.run_id,
            stop_reason="done",
            summary_chars=4,
            turns=1,
            max_turns=5,
            provider="mock",
        )
        before = len(self.log.entries(self.session_id))

        same = self.line.mark_terminal(
            self.session_id,
            self.run_id,
            stop_reason="done",
            summary_chars=4,
            turns=1,
            max_turns=5,
            provider="mock",
        )

        self.assertEqual(same.leaf, LEAF_TERMINAL)
        self.assertEqual(len(self.log.entries(self.session_id)), before)
        with self.assertRaises(RuntimeOperationTransitionError):
            self.line.mark_terminal(
                self.session_id,
                self.run_id,
                stop_reason="done",
                summary_chars=5,
                turns=1,
                max_turns=5,
                provider="mock",
            )

    def test_named_completion_and_repair_state_methods_commit_expected_leaves(self) -> None:
        self._accept_and_run_writer()

        settled = self.line.mark_writer_settled(
            self.session_id,
            self.run_id,
            provider_id="mock",
            turns_used=2,
            stop_reason="done",
        )
        proof = self.line.record_completion_proof(
            self.session_id,
            self.run_id,
            proof_ref="completion_proof:" + "b" * 16,
            proof_status="failed",
            proof_satisfied=False,
        )
        admitted = self.line.admit_repair_context(
            self.session_id,
            self.run_id,
            context_ref="sha256:" + "a" * 64,
        )
        running = self.line.mark_repair_running(
            self.session_id,
            self.run_id,
            provider_id="mock",
        )
        repair = self.line.mark_repair_settled(
            self.session_id,
            self.run_id,
            provider_id="mock",
            stop_reason="done",
            turns_used=3,
        )

        self.assertEqual(settled.leaf, LEAF_WRITER_SETTLED)
        self.assertEqual(proof.leaf, LEAF_COMPLETION_PROOF_RECORDED)
        self.assertEqual(admitted.leaf, LEAF_REPAIR_CONTEXT_ADMITTED)
        self.assertEqual(running.leaf, LEAF_REPAIR_RUNNING)
        self.assertEqual(repair.leaf, LEAF_REPAIR_SETTLED)
        self.assertEqual(self.operations.load(self.session_id, self.run_id).leaf, LEAF_REPAIR_SETTLED)

    def test_mark_completion_blocked_commits_blocked_verdict_without_new_leaf(self) -> None:
        self._accept_and_run_writer()
        self.line.mark_writer_settled(
            self.session_id,
            self.run_id,
            provider_id="mock",
            turns_used=2,
            stop_reason="done",
        )
        self.line.record_completion_proof(
            self.session_id,
            self.run_id,
            proof_ref="completion_proof:" + "c" * 16,
            proof_status="failed",
            proof_satisfied=False,
        )

        blocked = self.line.mark_completion_blocked(
            self.session_id,
            self.run_id,
            reason="verification_failed",
        )

        self.assertEqual(blocked.leaf, LEAF_COMPLETION_PROOF_RECORDED)
        self.assertEqual(blocked.blocked_reason, "verification_failed")


if __name__ == "__main__":
    unittest.main()
