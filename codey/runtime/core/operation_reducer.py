"""Pure durable-operation reducer.

The reducer is deliberately small: it dispatches on the flat operation-state
leaf and returns the only recovery action the runtime is allowed to execute.
It does not perform I/O and does not import business layers.
"""

from __future__ import annotations

from dataclasses import dataclass, replace

from codey.runtime.core.operation_payload import RuntimeExecutionCounters
from codey.runtime.core.operation_state import (
    LEAF_ACCEPTED,
    LEAF_CLOSING,
    LEAF_COMPLETION_PENDING,
    LEAF_COMPLETION_PROOF_RECORDED,
    LEAF_CONTROL_READY,
    LEAF_KERNEL_SETTLED,
    LEAF_PLAN_READY,
    LEAF_PROVIDER_EFFECT_PENDING,
    LEAF_PROVIDER_REQUEST_READY,
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
    operation_state_entry,
)
from codey.runtime.core.progress import ProgressSnapshot
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
ACTION_SUBMIT_PROVIDER_REQUEST = "submit_provider_request"
ACTION_PREPARE_TURN = "prepare_turn"
ACTION_EXECUTE_TOOL_PLAN = "execute_tool_plan"
ACTION_OBSERVE_BATCH_PROGRESS = "observe_batch_progress"
ACTION_EVALUATE_COMPLETION = "evaluate_completion"
ACTION_PREPARE_CONTROL_DELIVERY = "prepare_control_delivery"
ACTION_CLOSE_PROTOCOL = "close_protocol"
ACTION_RETURN_KERNEL_RESULT = "return_kernel_result"
ACTION_BEGIN_CLOSING = "begin_closing"
ACTION_RESUME_QUESTION = "resume_question"
ACTION_RETURN_WAITING = "return_waiting"
ACTION_RECONCILE_APPROVAL = "reconcile_approval"


@dataclass(frozen=True)
class RuntimeDecisionInputs:
    cancel_requested: bool = False
    closing_intent: bool = False
    question_answered: bool = False
    approval_ref: str = ""
    approval_ready: bool = False
    approval_outcome_unknown: bool = False


_NO_DECISION_INPUTS = RuntimeDecisionInputs()


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
    request_ref: str = ""
    work_ref: str = ""


@dataclass(frozen=True)
class OperationSettlement:
    abandoned_batch_ids: tuple[str, ...] = ()


def admit_request_counters(
    current: RuntimeExecutionCounters, proposed: RuntimeExecutionCounters,
) -> RuntimeExecutionCounters:
    proposed = RuntimeExecutionCounters.from_payload(proposed.to_payload())
    if (proposed.closing_round < current.closing_round
            or proposed.closing_round > current.closing_round + 1
            or (current.length_continuation_used and not proposed.length_continuation_used)
            or proposed.invalid_turns not in {0, current.invalid_turns, current.invalid_turns + 1}):
        raise RuntimeOperationTransitionError("request cannot reset or skip execution budgets")
    if (current.progress != proposed.progress and (current.progress.last_batch_id
            or replace(current.progress, limit=proposed.progress.limit) != proposed.progress)):
        raise RuntimeOperationTransitionError("request cannot replace committed batch progress")
    return proposed


def _progress_reference_valid(view: SessionView) -> bool:
    if view.state is None or not view.state.counters.progress.last_batch_id:
        return True
    batch = _batch_by_id(view.batches, view.state.counters.progress.last_batch_id)
    return batch is not None and _batch_fully_settled(batch, view.effects)


def record_progress_observation(
    view: SessionView, *, batch_id: str, expected: ProgressSnapshot, observed: ProgressSnapshot,
) -> tuple[dict[str, object], ...]:
    """Accept a bounded reduction of one complete original result batch."""
    state = view.state
    if state is None or state.leaf == LEAF_TERMINAL:
        raise RuntimeOperationTransitionError("progress requires an open operation")
    current = state.counters.progress
    if current.last_batch_id == batch_id:
        if current != observed:
            raise RuntimeOperationTransitionError("batch progress is immutable")
        return ()
    if current != expected or observed.last_batch_id != batch_id or not batch_id:
        raise RuntimeOperationTransitionError("progress observation has stale counters or identity")
    batch = _batch_by_id(view.batches, batch_id)
    if (state.leaf != LEAF_TOOL_DELIVERY_PENDING or pending_for(view).delivery_batch_id != batch_id
            or batch is None or not _batch_fully_settled(batch, view.effects)
            or any(_external_outcome_unknown(effect) for effect in view.effects)):
        raise RuntimeOperationTransitionError("progress requires the current complete result batch")
    counters = replace(state.counters, progress=ProgressSnapshot.from_payload(observed.to_payload()))
    return (operation_state_entry(replace(state, counters=counters)),)


def prepare_operation_settlement(view: SessionView) -> OperationSettlement:
    """Validate lifecycle settlement against receipts without inventing them.

    Stopping a scheduler does not establish any external outcome. Only a
    complete, known tool result batch may be explicitly abandoned here.
    """
    state = view.state
    if state is None:
        raise RuntimeOperationTransitionError("missing operation state")
    if not _progress_reference_valid(view):
        raise RuntimeOperationTransitionError("progress refers to an unavailable result batch")
    if any(effect.settlement is None for effect in view.effects):
        raise RuntimeOperationTransitionError("operation retains pending effects")
    if any(_external_outcome_unknown(effect) for effect in view.effects):
        raise RuntimeOperationTransitionError("operation retains unknown external outcomes")
    if state.leaf in {LEAF_PROVIDER_REQUEST_READY, LEAF_PROVIDER_EFFECT_PENDING, LEAF_PROVIDER_RESPONSE_READY, LEAF_TOOL_EFFECT_PENDING}:
        raise RuntimeOperationTransitionError("pending operation position has no matching unsettled effect")
    open_batches = tuple(batch for batch in view.batches if not batch.is_terminal)
    if state.leaf in {LEAF_TOOL_DELIVERY_PENDING, LEAF_KERNEL_SETTLED} and open_batches:
        batch = (open_batches[0] if state.leaf == LEAF_KERNEL_SETTLED
                 else _batch_by_id(view.batches, pending_for(view).delivery_batch_id))
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


def next_runtime_action(view: SessionView, inputs: RuntimeDecisionInputs = _NO_DECISION_INPUTS) -> RuntimeAction:
    guarded = _operation_guard_action(view, inputs)
    if guarded is not None:
        return guarded
    state = view.state
    assert state is not None
    pending = pending_for(view)
    unsettled = tuple(effect for effect in view.effects if effect.settlement is None)
    handlers = {
        LEAF_PROVIDER_REQUEST_READY: lambda: _prepared_request_action(state, unsettled),
        LEAF_PROVIDER_EFFECT_PENDING: lambda: _provider_pending_action(state, view.effects, pending),
        LEAF_PROVIDER_RESPONSE_READY: lambda: _response_ready_action(view),
        LEAF_TOOL_EFFECT_PENDING: lambda: (_approval_action(state, inputs, unsettled) if inputs.approval_ref
            else _tool_pending_action(state, view.effects, view.batches, pending)),
        LEAF_TOOL_DELIVERY_PENDING: lambda: _tool_delivery_action(state, view.effects, view.batches, pending, unsettled),
    }
    handler = handlers.get(state.leaf)
    if handler is not None:
        return handler()
    execution = _execution_position_action(view, inputs)
    return execution if execution is not None else _lifecycle_action(view)


def _operation_guard_action(view: SessionView, inputs: RuntimeDecisionInputs) -> RuntimeAction | None:
    state = view.state
    if state is None:
        return RuntimeAction(ACTION_FAIL_INVARIANT, reason="missing_operation_state")
    if state.leaf not in LEAVES:
        return RuntimeAction(ACTION_FAIL_INVARIANT, leaf=state.leaf, reason="unknown_leaf")
    if not _progress_reference_valid(view):
        return RuntimeAction(ACTION_FAIL_INVARIANT, leaf=state.leaf, reason="progress_batch_missing_or_incomplete")
    effects = view.effects
    unsettled = tuple(effect for effect in effects if effect.settlement is None)
    unknown = tuple(effect for effect in effects if _external_outcome_unknown(effect))
    if unknown:
        return RuntimeAction(ACTION_FAIL_INVARIANT, leaf=state.leaf, reason="external_outcome_unknown",
                             effect_ids=tuple(effect.intent.effect_id for effect in unknown))
    if (inputs.cancel_requested and not inputs.closing_intent and state.leaf in {
            LEAF_WRITER_RUNNING, LEAF_REPAIR_RUNNING, LEAF_PROVIDER_REQUEST_READY,
            LEAF_PROVIDER_RESPONSE_READY, LEAF_PLAN_READY, LEAF_COMPLETION_PENDING,
            LEAF_CONTROL_READY, LEAF_CLOSING, LEAF_KERNEL_SETTLED, LEAF_TOOL_DELIVERY_PENDING}):
        return RuntimeAction(ACTION_BEGIN_CLOSING, leaf=state.leaf, reason="stopped", driver=state.driver)

    if state.leaf == LEAF_TERMINAL:
        if unsettled:
            return RuntimeAction(ACTION_FAIL_INVARIANT, leaf=state.leaf, reason="terminal_retains_pending_effects")
        return RuntimeAction(ACTION_TERMINAL, leaf=state.leaf)

    return None


def _response_ready_action(view: SessionView) -> RuntimeAction:
    state = view.state
    assert state is not None
    providers = tuple(effect for effect in view.effects
                      if effect.intent.effect_category == EFFECT_CATEGORY_PROVIDER_SEND
                      and effect.intent.turn == state.turn)
    if (len(providers) != 1 or not state.response_ref
            or (providers[0].settlement is not None
                and providers[0].settlement.sent_state != "settled")):
        return RuntimeAction(ACTION_FAIL_INVARIANT, leaf=state.leaf, reason="missing_provider_response_fact")
    return RuntimeAction(ACTION_INTERPRET_PROVIDER_RESPONSE, leaf=state.leaf,
                         effect_id=providers[0].intent.effect_id, response_ref=state.response_ref, driver=state.driver)



def _execution_position_action(view: SessionView, inputs: RuntimeDecisionInputs) -> RuntimeAction | None:
    state = view.state
    assert state is not None
    if state.leaf == LEAF_KERNEL_SETTLED and inputs.question_answered:
        return RuntimeAction(ACTION_RESUME_QUESTION, leaf=state.leaf, work_ref=state.work_ref)

    execution_actions = {
        LEAF_PLAN_READY: ACTION_EXECUTE_TOOL_PLAN,
        LEAF_COMPLETION_PENDING: ACTION_EVALUATE_COMPLETION,
        LEAF_CONTROL_READY: ACTION_PREPARE_CONTROL_DELIVERY,
        LEAF_CLOSING: ACTION_CLOSE_PROTOCOL,
        LEAF_KERNEL_SETTLED: ACTION_RETURN_KERNEL_RESULT,
    }
    if state.leaf in execution_actions:
        if state.leaf in {LEAF_CONTROL_READY, LEAF_CLOSING, LEAF_KERNEL_SETTLED} and not state.work_ref:
            return RuntimeAction(ACTION_FAIL_INVARIANT, leaf=state.leaf, reason="execution_input_missing")
        return RuntimeAction(execution_actions[state.leaf], leaf=state.leaf, response_ref=state.response_ref,
                             work_ref=state.work_ref, driver=state.driver)

    return None


def _lifecycle_action(view: SessionView) -> RuntimeAction:
    state = view.state
    assert state is not None
    unsettled = tuple(effect for effect in view.effects if effect.settlement is None)
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
        if (state.leaf in {LEAF_WRITER_RUNNING, LEAF_REPAIR_RUNNING}
                and any(not batch.is_terminal for batch in view.batches)):
            return RuntimeAction(ACTION_FAIL_INVARIANT, leaf=state.leaf, reason="unexpected_undelivered_batch")
        return RuntimeAction(ACTION_PREPARE_TURN if state.leaf in {LEAF_WRITER_RUNNING, LEAF_REPAIR_RUNNING}
                             else ACTION_CONTINUE, leaf=state.leaf)

    return RuntimeAction(ACTION_FAIL_INVARIANT, leaf=state.leaf, reason="unhandled_leaf")


def _approval_action(state: RuntimeOperationState, inputs: RuntimeDecisionInputs,
                     unsettled: tuple[RuntimeEffectProjection, ...]) -> RuntimeAction:
    if inputs.approval_outcome_unknown:
        return RuntimeAction(ACTION_FAIL_INVARIANT, leaf=state.leaf, reason="approval_execution_outcome_unknown")
    if inputs.approval_ready or inputs.cancel_requested:
        return RuntimeAction(ACTION_RECONCILE_APPROVAL, leaf=state.leaf, effect_id=inputs.approval_ref,
                             reason="stopped" if inputs.cancel_requested else "")
    provider = tuple(effect for effect in unsettled if effect.intent.effect_category == EFFECT_CATEGORY_PROVIDER_SEND)
    if provider:
        return RuntimeAction(ACTION_COMMIT_GENERATION_BOUNDARY, leaf=state.leaf, reason="approval")
    return RuntimeAction(ACTION_RETURN_WAITING, leaf=state.leaf, reason="approval", effect_id=inputs.approval_ref)


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
    "ACTION_SUBMIT_PROVIDER_REQUEST",
    "RuntimeAction",
    "next_runtime_action",
    "OperationSettlement",
    "prepare_operation_settlement",
]


def _prepared_request_action(state: RuntimeOperationState, unsettled: tuple[RuntimeEffectProjection, ...]) -> RuntimeAction:
    providers = tuple(effect for effect in unsettled if effect.intent.effect_category == EFFECT_CATEGORY_PROVIDER_SEND)
    if len(providers) == 1 and len(unsettled) == 1 and state.response_ref:
        return RuntimeAction(ACTION_COMMIT_GENERATION_BOUNDARY, leaf=state.leaf,
            effect_id=providers[0].intent.effect_id, response_ref=state.response_ref,
            request_ref=state.request_ref, driver=state.driver, reason="before_provider_request")
    if unsettled or state.response_ref or not state.request_ref:
        return RuntimeAction(ACTION_FAIL_INVARIANT, leaf=state.leaf, reason="prepared_request_effects_mismatch")
    return RuntimeAction(ACTION_SUBMIT_PROVIDER_REQUEST, leaf=state.leaf,
                         request_ref=state.request_ref, driver=state.driver)


def _tool_delivery_action(state: RuntimeOperationState, effects: tuple[RuntimeEffectProjection, ...],
                          delivery_batches: tuple[DeliveryBatchProjection, ...], pending: PendingRuntimeFacts,
                          unsettled: tuple[RuntimeEffectProjection, ...]) -> RuntimeAction:
    batch = _batch_by_id(delivery_batches, pending.delivery_batch_id)
    if (batch is not None and _batch_fully_settled(batch, effects)
            and state.counters.progress.last_batch_id != batch.intent.batch_id):
        return RuntimeAction(ACTION_OBSERVE_BATCH_PROGRESS, leaf=state.leaf,
            effect_ids=batch.intent.tool_refs, delivery_batch_id=batch.intent.batch_id, driver=state.driver)
    providers = tuple(effect for effect in unsettled if effect.intent.effect_category == EFFECT_CATEGORY_PROVIDER_SEND)
    if providers:
        if len(providers) != 1 or len(unsettled) != 1:
            return RuntimeAction(ACTION_FAIL_INVARIANT, leaf=state.leaf, reason="generation_boundary_effects_mismatch")
        return RuntimeAction(ACTION_COMMIT_GENERATION_BOUNDARY, leaf=state.leaf,
                             effect_id=providers[0].intent.effect_id, reason="post_tools")
    return _delivery_pending_action(state, effects, delivery_batches, pending)
