"""Pure durable-operation reducer.

The reducer is deliberately small: it dispatches on the flat operation-state
leaf and returns the only recovery action the runtime is allowed to execute.
It does not perform I/O and does not import business layers.
"""

from __future__ import annotations

from dataclasses import dataclass

from codey.runtime.effect_records import RuntimeEffectProjection
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
    LEAVES,
    RuntimeOperationState,
)
from codey.runtime.replay_policy import ReplayClass, is_replayable_safe_tool
from codey.runtime.tool_result_delivery import DeliveryBatchProjection

ACTION_CONTINUE = "continue_operation"
ACTION_FAIL_INVARIANT = "fail_invariant"
ACTION_REPLAY_SAFE_TOOL_BATCH = "replay_safe_tool_batch"
ACTION_SETTLE_PROVIDER_UNKNOWN = "settle_provider_unknown"
ACTION_SYNTHESIZE_INTERRUPTED_EFFECTS = "synthesize_interrupted_effects"
ACTION_TERMINAL = "terminal"


@dataclass(frozen=True)
class RuntimeAction:
    kind: str
    leaf: str = ""
    reason: str = ""
    effect_id: str = ""
    effect_ids: tuple[str, ...] = ()
    delivery_batch_id: str = ""
    driver: str = ""


def next_runtime_action(
    state: RuntimeOperationState | None,
    *,
    effects: tuple[RuntimeEffectProjection, ...] = (),
    delivery_batches: tuple[DeliveryBatchProjection, ...] = (),
) -> RuntimeAction:
    if state is None:
        return RuntimeAction(ACTION_FAIL_INVARIANT, reason="missing_operation_state")
    if state.leaf not in LEAVES:
        return RuntimeAction(ACTION_FAIL_INVARIANT, leaf=state.leaf, reason="unknown_leaf")

    if state.leaf == LEAF_TERMINAL:
        return RuntimeAction(ACTION_TERMINAL, leaf=state.leaf)

    if state.leaf == LEAF_PROVIDER_EFFECT_PENDING:
        effect_id = _single_pending_effect_id(state)
        projection = _effect_by_id(effects, effect_id)
        if projection is None:
            return RuntimeAction(
                ACTION_FAIL_INVARIANT,
                leaf=state.leaf,
                reason="missing_provider_effect_intent",
                effect_id=effect_id,
            )
        if projection.settlement is not None:
            return RuntimeAction(
                ACTION_CONTINUE,
                leaf=state.leaf,
                reason="provider_already_settled",
                effect_id=effect_id,
                driver=state.driver,
            )
        return RuntimeAction(
            ACTION_SETTLE_PROVIDER_UNKNOWN,
            leaf=state.leaf,
            effect_id=effect_id,
            delivery_batch_id=state.pending_delivery_batch_id,
            driver=state.driver,
        )

    if state.leaf == LEAF_TOOL_EFFECT_PENDING:
        if not state.pending_effect_ids:
            return RuntimeAction(ACTION_FAIL_INVARIANT, leaf=state.leaf, reason="empty_tool_pending")
        projections = tuple(_effect_by_id(effects, effect_id) for effect_id in state.pending_effect_ids)
        missing = tuple(
            effect_id
            for effect_id, projection in zip(state.pending_effect_ids, projections)
            if projection is None
        )
        if missing:
            return RuntimeAction(
                ACTION_FAIL_INVARIANT,
                leaf=state.leaf,
                reason="missing_tool_effect_intent",
                effect_ids=missing,
                delivery_batch_id=state.pending_delivery_batch_id,
                driver=state.driver,
            )
        stale_settled = tuple(
            projection.intent.effect_id
            for projection in projections
            if projection is not None and projection.settlement is not None
        )
        if stale_settled:
            return RuntimeAction(
                ACTION_FAIL_INVARIANT,
                leaf=state.leaf,
                reason="settled_tool_effect_still_pending",
                effect_ids=stale_settled,
                delivery_batch_id=state.pending_delivery_batch_id,
                driver=state.driver,
            )
        if state.task_kind not in {"project", "hybrid"}:
            return RuntimeAction(
                ACTION_SYNTHESIZE_INTERRUPTED_EFFECTS,
                leaf=state.leaf,
                effect_ids=state.pending_effect_ids,
                delivery_batch_id=state.pending_delivery_batch_id,
                driver=state.driver,
                reason="task_kind_not_replayable",
            )
        batch = _batch_by_id(delivery_batches, state.pending_delivery_batch_id)
        if batch is None:
            return RuntimeAction(
                ACTION_FAIL_INVARIANT,
                leaf=state.leaf,
                reason="missing_delivery_batch",
                delivery_batch_id=state.pending_delivery_batch_id,
                driver=state.driver,
            )
        live_pending = tuple(
            projection
            for projection in projections
            if projection is not None
        )
        if batch.can_recover_before_provider_send and all(
            _safe_tool_projection(item) for item in live_pending
        ):
            return RuntimeAction(
                ACTION_REPLAY_SAFE_TOOL_BATCH,
                leaf=state.leaf,
                effect_ids=state.pending_effect_ids,
                delivery_batch_id=batch.intent.batch_id,
                driver=state.driver,
            )
        return RuntimeAction(
            ACTION_SYNTHESIZE_INTERRUPTED_EFFECTS,
            leaf=state.leaf,
            effect_ids=state.pending_effect_ids,
            delivery_batch_id=state.pending_delivery_batch_id,
            driver=state.driver,
            reason="tool_effect_not_replayable_as_batch",
        )

    if state.leaf == LEAF_TOOL_DELIVERY_PENDING:
        batch = _batch_by_id(delivery_batches, state.pending_delivery_batch_id)
        if batch is None:
            return RuntimeAction(
                ACTION_FAIL_INVARIANT,
                leaf=state.leaf,
                reason="missing_delivery_batch",
                delivery_batch_id=state.pending_delivery_batch_id,
                driver=state.driver,
            )
        if batch.can_recover_before_provider_send:
            return RuntimeAction(
                ACTION_REPLAY_SAFE_TOOL_BATCH,
                leaf=state.leaf,
                effect_ids=batch.intent.tool_refs,
                delivery_batch_id=batch.intent.batch_id,
                driver=state.driver,
            )
        return RuntimeAction(
            ACTION_CONTINUE,
            leaf=state.leaf,
            reason="tool_delivery_not_recoverable",
            delivery_batch_id=state.pending_delivery_batch_id,
            driver=state.driver,
        )

    if state.leaf in {
        LEAF_ACCEPTED,
        LEAF_WRITER_RUNNING,
        LEAF_WRITER_SETTLED,
        LEAF_COMPLETION_PROOF_RECORDED,
        LEAF_REPAIR_CONTEXT_ADMITTED,
        LEAF_REPAIR_RUNNING,
        LEAF_REPAIR_SETTLED,
    }:
        return RuntimeAction(ACTION_CONTINUE, leaf=state.leaf)

    return RuntimeAction(ACTION_FAIL_INVARIANT, leaf=state.leaf, reason="unhandled_leaf")


def _single_pending_effect_id(state: RuntimeOperationState) -> str:
    if len(state.pending_effect_ids) != 1:
        return ""
    return state.pending_effect_ids[0]


def _effect_by_id(
    effects: tuple[RuntimeEffectProjection, ...],
    effect_id: str,
) -> RuntimeEffectProjection | None:
    return next((effect for effect in effects if effect.intent.effect_id == effect_id), None)


def _batch_by_id(
    batches: tuple[DeliveryBatchProjection, ...],
    batch_id: str,
) -> DeliveryBatchProjection | None:
    return next((batch for batch in batches if batch.intent.batch_id == batch_id), None)


def _safe_tool_projection(projection: RuntimeEffectProjection) -> bool:
    intent = projection.intent
    return (
        intent.replay_class == ReplayClass.SAFE
        and is_replayable_safe_tool(intent.tool_name)
        and intent.replay_args is not None
    )


__all__ = [
    "ACTION_CONTINUE",
    "ACTION_FAIL_INVARIANT",
    "ACTION_REPLAY_SAFE_TOOL_BATCH",
    "ACTION_SETTLE_PROVIDER_UNKNOWN",
    "ACTION_SYNTHESIZE_INTERRUPTED_EFFECTS",
    "ACTION_TERMINAL",
    "RuntimeAction",
    "next_runtime_action",
]
