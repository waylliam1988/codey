"""Pure durable-operation reducer.

The reducer is deliberately small: it dispatches on the flat operation-state
leaf and returns the only recovery action the runtime is allowed to execute.
It does not perform I/O and does not import business layers.
"""

from __future__ import annotations

from dataclasses import dataclass

from codey.runtime.core.operation_state import (
    LEAF_ACCEPTED,
    LEAF_COMPLETION_PROOF_RECORDED,
    LEAF_PROVIDER_EFFECT_PENDING,
    LEAF_PROVIDER_RESPONSE_READY,
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
    RuntimeOperationTransitionError,
)
from codey.runtime.effects.effect_records import (
    EFFECT_CATEGORY_PROVIDER_SEND,
    EFFECT_CATEGORY_TOOL_CALL,
    RuntimeEffectProjection,
)
from codey.runtime.effects.replay_policy import ReplayClass, is_replayable_safe_tool
from codey.runtime.effects.tool_result_delivery import DeliveryBatchProjection
from codey.runtime.log.session_view import PendingRuntimeFacts, SessionView, pending_for

ACTION_CONTINUE = "continue_operation"
ACTION_FAIL_INVARIANT = "fail_invariant"
ACTION_REPLAY_SAFE_TOOL_BATCH = "replay_safe_tool_batch"
ACTION_REDELIVER_SETTLED_BATCH = "redeliver_settled_batch"
ACTION_SETTLE_PROVIDER_UNKNOWN = "settle_provider_unknown"
ACTION_SYNTHESIZE_INTERRUPTED_EFFECTS = "synthesize_interrupted_effects"
ACTION_TERMINAL = "terminal"
ACTION_INTERPRET_PROVIDER_RESPONSE = "interpret_provider_response"
ACTION_COMMIT_GENERATION_BOUNDARY = "commit_generation_boundary"


@dataclass(frozen=True)
class RuntimeAction:
    kind: str
    leaf: str = ""
    reason: str = ""
    effect_id: str = ""
    effect_ids: tuple[str, ...] = ()
    delivery_batch_id: str = ""
    driver: str = ""
    response_ref: str = ""


@dataclass(frozen=True)
class OperationSettlement:
    abandoned_batch_ids: tuple[str, ...] = ()


def prepare_operation_settlement(view: SessionView) -> OperationSettlement:
    """Validate lifecycle settlement against receipts without inventing them.

    Stopping a scheduler does not establish any external outcome. Only a
    complete, known tool result batch may be explicitly abandoned here.
    """
    state = view.state
    if state is None:
        raise RuntimeOperationTransitionError("missing operation state")
    if any(effect.settlement is None for effect in view.effects):
        raise RuntimeOperationTransitionError("operation retains pending effects")
    if any(_external_outcome_unknown(effect) for effect in view.effects):
        raise RuntimeOperationTransitionError("operation retains unknown external outcomes")
    if state.leaf in {LEAF_PROVIDER_EFFECT_PENDING, LEAF_PROVIDER_RESPONSE_READY, LEAF_TOOL_EFFECT_PENDING}:
        raise RuntimeOperationTransitionError("pending operation position has no matching unsettled effect")
    open_batches = tuple(batch for batch in view.batches if not batch.is_terminal)
    if state.leaf == LEAF_TOOL_DELIVERY_PENDING:
        batch = _batch_by_id(view.batches, pending_for(view).delivery_batch_id)
        if (batch is None or len(open_batches) != 1
                or not _batch_fully_settled(batch, view.effects)):
            raise RuntimeOperationTransitionError("operation has no unique complete result batch to abandon")
        return OperationSettlement((batch.intent.batch_id,))
    if open_batches:
        raise RuntimeOperationTransitionError("operation retains an unexpected undelivered batch")
    return OperationSettlement()


def _external_outcome_unknown(effect: RuntimeEffectProjection) -> bool:
    receipt = effect.settlement
    return receipt is not None and (
        receipt.sent_state == "maybe_sent" or receipt.error_code == "interrupted_by_crash"
    )


def next_runtime_action(view: SessionView) -> RuntimeAction:
    state = view.state
    if state is None:
        return RuntimeAction(ACTION_FAIL_INVARIANT, reason="missing_operation_state")
    if state.leaf not in LEAVES:
        return RuntimeAction(ACTION_FAIL_INVARIANT, leaf=state.leaf, reason="unknown_leaf")
    effects = view.effects
    delivery_batches = view.batches
    pending = pending_for(view)
    unsettled = tuple(effect for effect in effects if effect.settlement is None)
    unknown = tuple(effect for effect in effects if _external_outcome_unknown(effect))
    if unknown:
        return RuntimeAction(ACTION_FAIL_INVARIANT, leaf=state.leaf, reason="external_outcome_unknown",
                             effect_ids=tuple(effect.intent.effect_id for effect in unknown))

    if state.leaf == LEAF_TERMINAL:
        if unsettled:
            return RuntimeAction(ACTION_FAIL_INVARIANT, leaf=state.leaf, reason="terminal_retains_pending_effects")
        return RuntimeAction(ACTION_TERMINAL, leaf=state.leaf)

    if state.leaf == LEAF_PROVIDER_EFFECT_PENDING:
        return _provider_pending_action(state, effects, pending)

    if state.leaf == LEAF_PROVIDER_RESPONSE_READY:
        if len(pending.effect_ids) != 1 or not state.response_ref:
            return RuntimeAction(ACTION_FAIL_INVARIANT, leaf=state.leaf, reason="missing_provider_response_fact")
        return RuntimeAction(ACTION_INTERPRET_PROVIDER_RESPONSE, leaf=state.leaf,
                             effect_id=pending.effect_ids[0], response_ref=state.response_ref, driver=state.driver)

    if state.leaf == LEAF_TOOL_EFFECT_PENDING:
        return _tool_pending_action(state, effects, delivery_batches, pending)

    if state.leaf == LEAF_TOOL_DELIVERY_PENDING:
        providers = tuple(effect for effect in unsettled if effect.intent.effect_category == EFFECT_CATEGORY_PROVIDER_SEND)
        if providers:
            if len(providers) != 1 or len(unsettled) != 1:
                return RuntimeAction(ACTION_FAIL_INVARIANT, leaf=state.leaf, reason="generation_boundary_effects_mismatch")
            return RuntimeAction(ACTION_COMMIT_GENERATION_BOUNDARY, leaf=state.leaf,
                                 effect_id=providers[0].intent.effect_id, reason="post_tools")
        return _delivery_pending_action(state, effects, delivery_batches, pending)

    if state.leaf in {
        LEAF_ACCEPTED,
        LEAF_WRITER_RUNNING,
        LEAF_WRITER_SETTLED,
        LEAF_COMPLETION_PROOF_RECORDED,
        LEAF_REPAIR_CONTEXT_ADMITTED,
        LEAF_REPAIR_RUNNING,
        LEAF_REPAIR_SETTLED,
    }:
        if unsettled:
            return RuntimeAction(ACTION_FAIL_INVARIANT, leaf=state.leaf, reason="unexpected_pending_effects")
        return RuntimeAction(ACTION_CONTINUE, leaf=state.leaf)

    return RuntimeAction(ACTION_FAIL_INVARIANT, leaf=state.leaf, reason="unhandled_leaf")


def _provider_pending_action(
    state: RuntimeOperationState,
    effects: tuple[RuntimeEffectProjection, ...],
    pending: PendingRuntimeFacts,
) -> RuntimeAction:
    if len(pending.effect_ids) != 1:
        stale = tuple(
            effect.intent.effect_id
            for effect in effects
            if effect.intent.effect_category == EFFECT_CATEGORY_PROVIDER_SEND
            and effect.intent.turn == state.turn
            and effect.settlement is not None
        )
        if stale:
            return RuntimeAction(
                ACTION_FAIL_INVARIANT,
                leaf=state.leaf,
                reason="settled_provider_effect_still_pending",
                effect_id=stale[0],
                driver=state.driver,
            )
        return RuntimeAction(
            ACTION_FAIL_INVARIANT,
            leaf=state.leaf,
            reason="missing_provider_effect_intent",
            effect_id="",
        )
    effect_id = pending.effect_ids[0]
    return RuntimeAction(
        ACTION_SETTLE_PROVIDER_UNKNOWN,
        leaf=state.leaf,
        effect_id=effect_id,
        delivery_batch_id=pending.delivery_batch_id,
        driver=state.driver,
    )


def _tool_pending_action(
    state: RuntimeOperationState,
    effects: tuple[RuntimeEffectProjection, ...],
    delivery_batches: tuple[DeliveryBatchProjection, ...],
    pending: PendingRuntimeFacts,
) -> RuntimeAction:
    if not pending.effect_ids:
        stale_settled = tuple(
            effect.intent.effect_id
            for effect in effects
            if effect.intent.effect_category == EFFECT_CATEGORY_TOOL_CALL
            and effect.intent.turn == state.turn
            and effect.settlement is not None
        )
        if stale_settled:
            return RuntimeAction(
                ACTION_FAIL_INVARIANT,
                leaf=state.leaf,
                reason="settled_tool_effect_still_pending",
                effect_ids=stale_settled,
                delivery_batch_id=pending.delivery_batch_id,
                driver=state.driver,
            )
        return RuntimeAction(ACTION_FAIL_INVARIANT, leaf=state.leaf, reason="empty_tool_pending")
    projections = tuple(_effect_by_id(effects, effect_id) for effect_id in pending.effect_ids)
    live_pending = tuple(projection for projection in projections if projection is not None)
    if len(live_pending) != len(pending.effect_ids):
        return RuntimeAction(
            ACTION_FAIL_INVARIANT,
            leaf=state.leaf,
            reason="missing_tool_effect_intent",
            effect_ids=pending.effect_ids,
            delivery_batch_id=pending.delivery_batch_id,
            driver=state.driver,
        )
    if state.task_kind not in {"project", "hybrid"}:
        return RuntimeAction(
            ACTION_SYNTHESIZE_INTERRUPTED_EFFECTS,
            leaf=state.leaf,
            effect_ids=pending.effect_ids,
            delivery_batch_id=pending.delivery_batch_id,
            driver=state.driver,
            reason="task_kind_not_replayable",
        )
    batch = _batch_by_id(delivery_batches, pending.delivery_batch_id)
    if batch is None:
        return RuntimeAction(
            ACTION_FAIL_INVARIANT,
            leaf=state.leaf,
            reason="missing_delivery_batch",
            delivery_batch_id=pending.delivery_batch_id,
            driver=state.driver,
        )
    if batch.can_recover_before_provider_send and all(
        _safe_tool_projection(item) for item in live_pending
    ):
        return RuntimeAction(
            ACTION_REPLAY_SAFE_TOOL_BATCH,
            leaf=state.leaf,
            effect_ids=pending.effect_ids,
            delivery_batch_id=batch.intent.batch_id,
            driver=state.driver,
        )
    return RuntimeAction(
        ACTION_SYNTHESIZE_INTERRUPTED_EFFECTS,
        leaf=state.leaf,
        effect_ids=pending.effect_ids,
        delivery_batch_id=pending.delivery_batch_id,
        driver=state.driver,
        reason="tool_effect_not_replayable_as_batch",
    )


def _delivery_pending_action(
    state: RuntimeOperationState,
    effects: tuple[RuntimeEffectProjection, ...],
    delivery_batches: tuple[DeliveryBatchProjection, ...],
    pending: PendingRuntimeFacts,
) -> RuntimeAction:
    batch = _batch_by_id(delivery_batches, pending.delivery_batch_id)
    if batch is None:
        return RuntimeAction(
            ACTION_FAIL_INVARIANT,
            leaf=state.leaf,
            reason="missing_delivery_batch",
            delivery_batch_id=pending.delivery_batch_id,
            driver=state.driver,
        )
    if _batch_fully_settled(batch, effects):
        # 已结算结果的重发优先于重放：有原结果就不重新执行，
        # 不调用执行器，不依赖 ReplayClass.SAFE（与未结算工具的
        # 执行恢复严格分离）。
        return RuntimeAction(
            ACTION_REDELIVER_SETTLED_BATCH,
            leaf=state.leaf,
            effect_ids=batch.intent.tool_refs,
            delivery_batch_id=batch.intent.batch_id,
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
        delivery_batch_id=pending.delivery_batch_id,
        driver=state.driver,
    )


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


def _batch_fully_settled(
    batch: DeliveryBatchProjection,
    effects: tuple[RuntimeEffectProjection, ...],
) -> bool:
    """True when every batch effect carries a settlement receipt.

    纯状态判断：收据内容是否可重建由恢复层校验（fail-closed），reducer
    只决定“重发”动作，不触及执行器。
    """
    if not batch.intent.tool_refs:
        return False
    by_id = {effect.intent.effect_id: effect for effect in effects}
    for effect_id in batch.intent.tool_refs:
        projection = by_id.get(effect_id)
        if projection is None or projection.settlement is None:
            return False
    return True


__all__ = [
    "ACTION_CONTINUE",
    "ACTION_FAIL_INVARIANT",
    "ACTION_REPLAY_SAFE_TOOL_BATCH",
    "ACTION_REDELIVER_SETTLED_BATCH",
    "ACTION_SETTLE_PROVIDER_UNKNOWN",
    "ACTION_SYNTHESIZE_INTERRUPTED_EFFECTS",
    "ACTION_TERMINAL",
    "ACTION_INTERPRET_PROVIDER_RESPONSE",
    "ACTION_COMMIT_GENERATION_BOUNDARY",
    "RuntimeAction",
    "next_runtime_action",
    "OperationSettlement",
    "prepare_operation_settlement",
]
