from __future__ import annotations

import unittest

from codey.runtime.effect_records import (
    EFFECT_CATEGORY_PROVIDER_SEND,
    EFFECT_CATEGORY_TOOL_CALL,
    RuntimeEffectIntent,
    RuntimeEffectProjection,
    RuntimeEffectSettlement,
)
from codey.runtime.operation_reducer import (
    ACTION_CONTINUE,
    ACTION_FAIL_INVARIANT,
    ACTION_REPLAY_SAFE_TOOL_BATCH,
    ACTION_SETTLE_PROVIDER_UNKNOWN,
    ACTION_SYNTHESIZE_INTERRUPTED_EFFECTS,
    ACTION_TERMINAL,
    next_runtime_action,
)
from codey.runtime.operation_state import (
    DRIVER_WRITER,
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
    LEAVES,
    RuntimeOperationState,
    lane_for_run,
    operation_id_for_run,
)
from codey.runtime.replay_policy import ReplayClass
from codey.runtime.tool_result_delivery import (
    DeliveryBatchIntent,
    DeliveryBatchItem,
    DeliveryBatchProjection,
    compute_batch_digest,
)


SESSION_ID = "sess-reducer"
RUN_ID = "run-reducer"
LANE = lane_for_run(RUN_ID)
OPERATION_ID = operation_id_for_run(RUN_ID)


def _state(leaf: str, **overrides: object) -> RuntimeOperationState:
    values = {
        "session_id": SESSION_ID,
        "run_id": RUN_ID,
        "operation_id": OPERATION_ID,
        "lane": LANE,
        "project_ref": "",
        "provider_id": "mock",
        "turn_budget": 5,
        "max_repair_rounds": 1,
        "leaf": leaf,
        "started_at": "2026-01-01T00:00:00Z",
        "updated_at": "2026-01-01T00:00:01Z",
        "task_kind": "project",
    }
    values.update(overrides)
    return RuntimeOperationState(**values)


def _provider_projection(
    effect_id: str,
    *,
    settled: bool = False,
) -> RuntimeEffectProjection:
    intent = RuntimeEffectIntent(
        effect_id=effect_id,
        effect_category=EFFECT_CATEGORY_PROVIDER_SEND,
        session_id=SESSION_ID,
        run_id=RUN_ID,
        lane=LANE,
        operation_id=OPERATION_ID,
        phase=DRIVER_WRITER,
        provider_id="mock",
        turn=1,
        replay_class=ReplayClass.UNSAFE,
    )
    settlement = (
        RuntimeEffectSettlement(
            effect_id=effect_id,
            effect_category=EFFECT_CATEGORY_PROVIDER_SEND,
            session_id=SESSION_ID,
            run_id=RUN_ID,
            lane=LANE,
            operation_id=OPERATION_ID,
            replay_class=ReplayClass.UNSAFE,
        )
        if settled
        else None
    )
    return RuntimeEffectProjection(intent=intent, settlement=settlement)


def _tool_projection(
    effect_id: str,
    *,
    tool_name: str = "read",
    replay_class: str = ReplayClass.SAFE,
    replay_args: dict[str, object] | None = None,
    settled: bool = False,
) -> RuntimeEffectProjection:
    if replay_args is None and replay_class == ReplayClass.SAFE and tool_name == "read":
        replay_args = {"path": "target.py"}
    intent = RuntimeEffectIntent(
        effect_id=effect_id,
        effect_category=EFFECT_CATEGORY_TOOL_CALL,
        session_id=SESSION_ID,
        run_id=RUN_ID,
        lane=LANE,
        operation_id=OPERATION_ID,
        phase=DRIVER_WRITER,
        turn=1,
        tool_index=0,
        tool_name=tool_name,
        replay_class=replay_class,
        replay_args=replay_args,
    )
    settlement = (
        RuntimeEffectSettlement(
            effect_id=effect_id,
            effect_category=EFFECT_CATEGORY_TOOL_CALL,
            session_id=SESSION_ID,
            run_id=RUN_ID,
            lane=LANE,
            operation_id=OPERATION_ID,
            replay_class=replay_class,
        )
        if settled
        else None
    )
    return RuntimeEffectProjection(intent=intent, settlement=settlement)


def _batch(
    batch_id: str,
    items: tuple[DeliveryBatchItem, ...],
    *,
    send_attempts: tuple[str, ...] = (),
    delivered_effect_ids: tuple[str, ...] = (),
) -> DeliveryBatchProjection:
    intent = DeliveryBatchIntent(
        batch_id=batch_id,
        session_id=SESSION_ID,
        run_id=RUN_ID,
        lane=LANE,
        operation_id=OPERATION_ID,
        turn=1,
        items=items,
        batch_digest=compute_batch_digest(items),
    )
    return DeliveryBatchProjection(
        intent=intent,
        send_attempts=send_attempts,
        delivered_effect_ids=delivered_effect_ids,
        is_delivered=bool(delivered_effect_ids),
    )


class RuntimeOperationReducerTests(unittest.TestCase):
    def test_missing_state_fails_closed(self) -> None:
        action = next_runtime_action(None)

        self.assertEqual(action.kind, ACTION_FAIL_INVARIANT)
        self.assertEqual(action.reason, "missing_operation_state")

    def test_non_pending_leaves_continue_or_terminal(self) -> None:
        expected = {
            LEAF_ACCEPTED: ACTION_CONTINUE,
            LEAF_WRITER_RUNNING: ACTION_CONTINUE,
            LEAF_WRITER_SETTLED: ACTION_CONTINUE,
            LEAF_COMPLETION_PROOF_RECORDED: ACTION_CONTINUE,
            LEAF_REPAIR_CONTEXT_ADMITTED: ACTION_CONTINUE,
            LEAF_REPAIR_RUNNING: ACTION_CONTINUE,
            LEAF_REPAIR_SETTLED: ACTION_CONTINUE,
            LEAF_TERMINAL: ACTION_TERMINAL,
        }

        for leaf, kind in expected.items():
            with self.subTest(leaf=leaf):
                action = next_runtime_action(_state(leaf))
                self.assertEqual(action.kind, kind)
                self.assertEqual(action.leaf, leaf)

    def test_provider_pending_without_settlement_records_unknown_provider_outcome(self) -> None:
        state = _state(
            LEAF_PROVIDER_EFFECT_PENDING,
            driver=DRIVER_WRITER,
            pending_effect_category=EFFECT_CATEGORY_PROVIDER_SEND,
            pending_effect_ids=("eff-provider",),
            pending_delivery_batch_id="batch-1",
        )

        action = next_runtime_action(
            state,
            effects=(_provider_projection("eff-provider"),),
        )

        self.assertEqual(action.kind, ACTION_SETTLE_PROVIDER_UNKNOWN)
        self.assertEqual(action.effect_id, "eff-provider")
        self.assertEqual(action.delivery_batch_id, "batch-1")
        self.assertEqual(action.driver, DRIVER_WRITER)

    def test_provider_pending_missing_intent_fails_invariant(self) -> None:
        action = next_runtime_action(
            _state(
                LEAF_PROVIDER_EFFECT_PENDING,
                driver=DRIVER_WRITER,
                pending_effect_category=EFFECT_CATEGORY_PROVIDER_SEND,
                pending_effect_ids=("missing-provider",),
            )
        )

        self.assertEqual(action.kind, ACTION_FAIL_INVARIANT)
        self.assertEqual(action.reason, "missing_provider_effect_intent")

    def test_provider_pending_with_existing_settlement_continues(self) -> None:
        state = _state(
            LEAF_PROVIDER_EFFECT_PENDING,
            driver=DRIVER_WRITER,
            pending_effect_category=EFFECT_CATEGORY_PROVIDER_SEND,
            pending_effect_ids=("eff-provider",),
        )

        action = next_runtime_action(
            state,
            effects=(_provider_projection("eff-provider", settled=True),),
        )

        self.assertEqual(action.kind, ACTION_CONTINUE)
        self.assertEqual(action.reason, "provider_already_settled")

    def test_tool_pending_replays_all_safe_batch_before_provider_send(self) -> None:
        items = (
            DeliveryBatchItem(0, "read", "eff-read", "safe", False),
            DeliveryBatchItem(1, "search", "eff-search", "safe", False),
        )
        state = _state(
            LEAF_TOOL_EFFECT_PENDING,
            driver=DRIVER_WRITER,
            pending_effect_category=EFFECT_CATEGORY_TOOL_CALL,
            pending_effect_ids=("eff-read", "eff-search"),
            pending_delivery_batch_id="batch-safe",
            turn=1,
        )

        action = next_runtime_action(
            state,
            effects=(
                _tool_projection("eff-read", tool_name="read", replay_args={"path": "target.py"}),
                _tool_projection(
                    "eff-search",
                    tool_name="search",
                    replay_args={"path": ".", "query": "target"},
                ),
            ),
            delivery_batches=(_batch("batch-safe", items),),
        )

        self.assertEqual(action.kind, ACTION_REPLAY_SAFE_TOOL_BATCH)
        self.assertEqual(action.effect_ids, ("eff-read", "eff-search"))
        self.assertEqual(action.delivery_batch_id, "batch-safe")

    def test_tool_pending_fails_when_delivery_batch_is_missing(self) -> None:
        action = next_runtime_action(
            _state(
                LEAF_TOOL_EFFECT_PENDING,
                driver=DRIVER_WRITER,
                pending_effect_category=EFFECT_CATEGORY_TOOL_CALL,
                pending_effect_ids=("eff-read",),
                pending_delivery_batch_id="missing-batch",
                turn=1,
            ),
            effects=(_tool_projection("eff-read"),),
        )

        self.assertEqual(action.kind, ACTION_FAIL_INVARIANT)
        self.assertEqual(action.reason, "missing_delivery_batch")

    def test_tool_pending_synthesizes_when_batch_is_not_replayable(self) -> None:
        items = (
            DeliveryBatchItem(0, "read", "eff-read", "safe", False),
            DeliveryBatchItem(1, "shell", "denied:shell:1", "unsafe", True),
        )
        state = _state(
            LEAF_TOOL_EFFECT_PENDING,
            driver=DRIVER_WRITER,
            pending_effect_category=EFFECT_CATEGORY_TOOL_CALL,
            pending_effect_ids=("eff-read",),
            pending_delivery_batch_id="batch-denied",
            turn=1,
        )

        action = next_runtime_action(
            state,
            effects=(_tool_projection("eff-read"),),
            delivery_batches=(_batch("batch-denied", items),),
        )

        self.assertEqual(action.kind, ACTION_SYNTHESIZE_INTERRUPTED_EFFECTS)
        self.assertEqual(action.reason, "tool_effect_not_replayable_as_batch")
        self.assertEqual(action.effect_ids, ("eff-read",))

    def test_tool_pending_synthesizes_for_non_project_task_kind(self) -> None:
        state = _state(
            LEAF_TOOL_EFFECT_PENDING,
            driver=DRIVER_WRITER,
            pending_effect_category=EFFECT_CATEGORY_TOOL_CALL,
            pending_effect_ids=("eff-read",),
            pending_delivery_batch_id="batch-safe",
            task_kind="chat",
            turn=1,
        )

        action = next_runtime_action(state, effects=(_tool_projection("eff-read"),))

        self.assertEqual(action.kind, ACTION_SYNTHESIZE_INTERRUPTED_EFFECTS)
        self.assertEqual(action.reason, "task_kind_not_replayable")

    def test_tool_pending_fails_if_state_still_points_at_settled_effect(self) -> None:
        state = _state(
            LEAF_TOOL_EFFECT_PENDING,
            driver=DRIVER_WRITER,
            pending_effect_category=EFFECT_CATEGORY_TOOL_CALL,
            pending_effect_ids=("eff-read",),
            pending_delivery_batch_id="batch-safe",
            turn=1,
        )

        action = next_runtime_action(state, effects=(_tool_projection("eff-read", settled=True),))

        self.assertEqual(action.kind, ACTION_FAIL_INVARIANT)
        self.assertEqual(action.reason, "settled_tool_effect_still_pending")

    def test_delivery_pending_replays_safe_undelivered_batch(self) -> None:
        items = (DeliveryBatchItem(0, "read", "eff-read", "safe", False),)
        state = _state(
            LEAF_TOOL_DELIVERY_PENDING,
            driver=DRIVER_WRITER,
            pending_delivery_batch_id="batch-safe",
            turn=1,
        )

        action = next_runtime_action(
            state,
            delivery_batches=(_batch("batch-safe", items),),
        )

        self.assertEqual(action.kind, ACTION_REPLAY_SAFE_TOOL_BATCH)
        self.assertEqual(action.effect_ids, ("eff-read",))

    def test_delivery_pending_continues_after_provider_send_attempt(self) -> None:
        items = (DeliveryBatchItem(0, "read", "eff-read", "safe", False),)
        state = _state(
            LEAF_TOOL_DELIVERY_PENDING,
            driver=DRIVER_WRITER,
            pending_delivery_batch_id="batch-safe",
            turn=1,
        )

        action = next_runtime_action(
            state,
            delivery_batches=(_batch("batch-safe", items, send_attempts=("eff-provider",)),),
        )

        self.assertEqual(action.kind, ACTION_CONTINUE)
        self.assertEqual(action.reason, "tool_delivery_not_recoverable")

    def test_all_known_leaves_have_total_dispatch(self) -> None:
        fixtures: dict[str, dict[str, object]] = {
            LEAF_PROVIDER_EFFECT_PENDING: {
                "driver": DRIVER_WRITER,
                "pending_effect_category": EFFECT_CATEGORY_PROVIDER_SEND,
                "pending_effect_ids": ("missing-provider",),
            },
            LEAF_TOOL_EFFECT_PENDING: {
                "driver": DRIVER_WRITER,
                "pending_effect_category": EFFECT_CATEGORY_TOOL_CALL,
                "pending_effect_ids": ("missing-tool",),
                "pending_delivery_batch_id": "missing-batch",
                "turn": 1,
            },
            LEAF_TOOL_DELIVERY_PENDING: {
                "driver": DRIVER_WRITER,
                "pending_delivery_batch_id": "missing-batch",
                "turn": 1,
            },
        }

        for leaf in sorted(LEAVES):
            with self.subTest(leaf=leaf):
                action = next_runtime_action(_state(leaf, **fixtures.get(leaf, {})))
                self.assertNotEqual(action.reason, "unhandled_leaf")


if __name__ == "__main__":
    unittest.main()
