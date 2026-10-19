from __future__ import annotations

import unittest

from codey.durable import DurableSession, MemoryDurableStore
from codey.runtime.core.operation_reducer import (
    ACTION_CONTINUE,
    ACTION_REPLAY_SAFE_TOOL_BATCH,
    ACTION_SETTLE_PROVIDER_UNKNOWN,
)
from codey.runtime.core.operation_state import DRIVER_WRITER
from codey.runtime.effects.effect_records import (
    EFFECT_CATEGORY_PROVIDER_SEND,
    EFFECT_CATEGORY_TOOL_CALL,
    RuntimeEffectIntent,
    new_effect_id,
)
from codey.runtime.effects.replay_policy import ReplayClass
from codey.runtime.effects.tool_result_delivery import (
    DeliveryBatchIntent,
    DeliveryBatchItem,
    ToolResultDeliveryStore,
    compute_batch_digest,
)
from codey.runtime.write.drive import peek_next_action
from codey.runtime.write.mutation_line import RuntimeMutationLine


class RuntimeDriveTests(unittest.TestCase):
    def setUp(self) -> None:
        self.durable = DurableSession(MemoryDurableStore())
        self.line = RuntimeMutationLine(durable=self.durable)
        self.delivery = ToolResultDeliveryStore(durable=self.durable)
        self.session_id = "sess-drive"
        self.run_id = "run-drive"
        self.durable.admit_input(
            conversation_id=self.session_id,
            request_id="request-drive",
            task_id=self.run_id,
            text="drive",
            provider_snapshot={"provider_id": "mock", "model_id": "mock"},
        )
        self.durable.claim_task(self.run_id)
        self.durable.start_task(self.run_id, state=dict(self.durable.task(self.run_id).state))
        self.line.accept_operation(
            session_id=self.session_id,
            run_id=self.run_id,
            project="",
            provider_id="mock",
            turn_budget=5,
            max_repair_rounds=1,
            task_kind="project",
        )

    def tearDown(self) -> None:
        self.durable.close()

    def _mark_writer_running(self) -> None:
        self.line.mark_writer_running(
            self.session_id,
            self.run_id,
            provider_id="mock",
        )

    def test_peek_continue_action_does_not_commit(self) -> None:
        before = self.durable.task_facts(self.run_id)

        first = peek_next_action(
            self.durable,
            session_id=self.session_id,
            run_id=self.run_id,
        )
        second = peek_next_action(
            self.durable,
            session_id=self.session_id,
            run_id=self.run_id,
        )

        self.assertEqual(first.kind, ACTION_CONTINUE)
        self.assertEqual(first, second)
        self.assertEqual(self.durable.task_facts(self.run_id), before)

    def test_peek_provider_pending_action_from_durable_state(self) -> None:
        self._mark_writer_running()
        effect_id = new_effect_id(EFFECT_CATEGORY_PROVIDER_SEND, self.run_id)
        self.line.begin_provider_effect(
            self.session_id,
            self.run_id,
            RuntimeEffectIntent(
                effect_id=effect_id,
                effect_category=EFFECT_CATEGORY_PROVIDER_SEND,
                session_id=self.session_id,
                run_id=self.run_id,
                phase=DRIVER_WRITER,
                provider_id="mock",
                turn=1,
                args_digest="provider-digest",
                replay_class=ReplayClass.UNSAFE,
            ),
            driver=DRIVER_WRITER,
        )
        before = self.durable.task_facts(self.run_id)

        action = peek_next_action(
            self.durable,
            session_id=self.session_id,
            run_id=self.run_id,
        )

        self.assertEqual(action.kind, ACTION_SETTLE_PROVIDER_UNKNOWN)
        self.assertEqual(action.effect_id, effect_id)
        self.assertEqual(self.durable.task_facts(self.run_id), before)

    def test_peek_tool_pending_action_from_effect_and_delivery_ledgers(self) -> None:
        self._mark_writer_running()
        self.durable.advance_task(
            task_id=self.run_id,
            phase="assistant_response_pending",
            state={},
        )
        effect_id = new_effect_id(EFFECT_CATEGORY_TOOL_CALL, self.run_id)
        items = (DeliveryBatchItem(0, "read", effect_id, "safe", False),)
        self.line.begin_tool_batch(
            self.session_id,
            self.run_id,
            intents=(
                RuntimeEffectIntent(
                    effect_id=effect_id,
                    effect_category=EFFECT_CATEGORY_TOOL_CALL,
                    session_id=self.session_id,
                    run_id=self.run_id,
                    phase=DRIVER_WRITER,
                    turn=1,
                    tool_index=0,
                    tool_name="read",
                    args_digest="read-digest",
                    delivery_batch_id="batch-drive",
                    replay_class=ReplayClass.SAFE,
                    replay_args={"path": "target.py"},
                ),
            ),
            delivery_intent=DeliveryBatchIntent(
                batch_id="batch-drive",
                session_id=self.session_id,
                run_id=self.run_id,
                turn=1,
                items=items,
                batch_digest=compute_batch_digest(items),
            ),
        )
        before = self.durable.task_facts(self.run_id)

        action = peek_next_action(
            self.durable,
            session_id=self.session_id,
            run_id=self.run_id,
        )

        self.assertEqual(action.kind, ACTION_REPLAY_SAFE_TOOL_BATCH)
        self.assertEqual(action.effect_ids, (effect_id,))
        self.assertEqual(action.delivery_batch_id, "batch-drive")
        self.assertEqual(self.durable.task_facts(self.run_id), before)
        self.assertTrue(self.delivery.load_batches(self.session_id, self.run_id)[0].can_recover_before_provider_send)


if __name__ == "__main__":
    unittest.main()
