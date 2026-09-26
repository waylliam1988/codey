"""Red-first: abandoned delivery must have an explicit terminal record.

Covers review item 2: writer/repair settle from tool_delivery_pending must
close the batch with `abandoned` (not delivered/recovered), and replay must
not treat it as pending.
"""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from codey.runtime.core.operation_state import LEAF_WRITER_SETTLED, RuntimeOperationTransitionError
from codey.runtime.effects.effect_records import (
    EFFECT_CATEGORY_TOOL_CALL,
    SETTLEMENT_STATUS_OK,
    RuntimeEffectIntent,
    RuntimeEffectSettlement,
    new_effect_id,
)
from codey.runtime.effects.replay_policy import ReplayClass
from codey.runtime.effects.tool_result_delivery import (
    DeliveryBatchIntent,
    DeliveryBatchItem,
    ToolResultDeliveryStore,
    compute_batch_digest,
    new_batch_id,
)
from codey.runtime.log.session_log import RuntimeSessionLog
from codey.runtime.write.mutation_line import RuntimeMutationLine


class DeliveryAbandonedContractTests(unittest.TestCase):
    def _drive_to_delivery_pending(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        log = RuntimeSessionLog(Path(tmp.name))
        line = RuntimeMutationLine(log)
        sid, rid = "s-abandon", "r-abandon-1"
        line.accept_operation(
            session_id=sid, run_id=rid, provider_id="mock",
            turn_budget=5, max_repair_rounds=1, task_kind="project",
        )
        line.mark_writer_running(session_id=sid, run_id=rid, provider_id="mock")
        eid = new_effect_id(EFFECT_CATEGORY_TOOL_CALL, rid)
        intent = RuntimeEffectIntent(
            effect_id=eid, effect_category=EFFECT_CATEGORY_TOOL_CALL,
            session_id=sid, run_id=rid, phase="writer", turn=1,
            tool_index=0, tool_name="read",
            replay_class=ReplayClass.SAFE, replay_args={"path": "target.txt"},
        )
        items = (DeliveryBatchItem(0, "read", eid, "safe", False),)
        batch_id = new_batch_id(rid, 1)
        line.begin_tool_batch(
            session_id=sid, run_id=rid, intents=(intent,),
            delivery_intent=DeliveryBatchIntent(
                batch_id=batch_id, session_id=sid, run_id=rid, turn=1,
                items=items, batch_digest=compute_batch_digest(items),
            ),
        )
        line.settle_tool_effect(
            session_id=sid, run_id=rid,
            settlement=RuntimeEffectSettlement(
                effect_id=eid, effect_category=EFFECT_CATEGORY_TOOL_CALL,
                session_id=sid, run_id=rid, status=SETTLEMENT_STATUS_OK,
                sent_state="settled", replay_class=ReplayClass.SAFE,
            ),
        )
        return log, line, sid, rid, batch_id

    def test_writer_settle_abandons_pending_batch(self) -> None:
        log, line, sid, rid, batch_id = self._drive_to_delivery_pending()
        settled = line.mark_writer_settled(
            session_id=sid, run_id=rid, provider_id="mock",
            turns_used=4, stop_reason="stopped",
        )
        assert settled is not None
        self.assertEqual(settled.leaf, LEAF_WRITER_SETTLED)
        batches = ToolResultDeliveryStore(log).load_batches(sid, rid)
        batch = next(b for b in batches if b.intent.batch_id == batch_id)
        # Operation settled AND batch explicitly terminal (abandoned).
        self.assertTrue(batch.is_abandoned)
        self.assertFalse(batch.is_delivered)
        self.assertFalse(batch.is_recovered)
        # Abandoned is terminal: not replayable, not recoverable.
        self.assertFalse(batch.can_recover_before_provider_send)
        self.assertEqual(
            ToolResultDeliveryStore(log).undelivered_replayable_batches(sid, rid), ()
        )

    def test_abandoned_batch_rejects_recovery_and_delivery(self) -> None:
        log, line, sid, rid, batch_id = self._drive_to_delivery_pending()
        line.mark_writer_settled(
            session_id=sid, run_id=rid, provider_id="mock",
            turns_used=4, stop_reason="stopped",
        )
        with self.assertRaises(RuntimeOperationTransitionError):
            line.record_delivery_recovered(
                session_id=sid, run_id=rid, batch_id=batch_id,
                recovered_effect_ids=("eff-x",),
            )


if __name__ == "__main__":
    unittest.main()
