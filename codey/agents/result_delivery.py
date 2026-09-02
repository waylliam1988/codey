"""Tool result delivery coordinator and prompt dispatch.

Unifies the formatting, context injection, durable delivery receipt recording,
and outbound provider transmission of tool results.

Does not import operations, ghost, or web providers.
"""

from __future__ import annotations

from codey.agents.prompt_context import append_coding_context, send_prompt
from codey.agents.state import AgentLoopSession
from codey.agents.tool_execution import TurnState
from codey.runtime.effects import lane_for_run, operation_id_for_run
from codey.runtime.tool_result_delivery import (
    DeliveryBatchIntent,
    DeliveryBatchItem,
    compute_batch_digest,
    new_batch_id,
)


def ensure_result_batch_intent(
    session: AgentLoopSession,
    turn_state: TurnState,
    turn: int,
) -> str:
    """Find existing matching batch intent or record durable batch intent (fail-closed)."""
    delivery_store = session.tool_result_delivery
    if (
        delivery_store is None
        or not session.session_id
        or not session.run_id
        or not turn_state.delivery_items
    ):
        return ""

    items = tuple(
        DeliveryBatchItem(
            tool_index=item.tool_index,
            tool_name=item.tool_name,
            ref=item.ref or item.effect_id or f"item:{item.turn}:{item.tool_index}:{item.tool_name}",
            replay_class=item.replay_class,
            is_denied=item.is_denied,
        )
        for item in turn_state.delivery_items
    )
    expected_digest = compute_batch_digest(items)

    # Check if an undelivered batch with EXACT matching items/digest was already planned
    batches = delivery_store.load_batches(session.session_id, session.run_id)
    for b in reversed(batches):
        if (
            b.intent.turn == turn
            and b.intent.batch_digest == expected_digest
            and not b.is_delivered
            and not b.send_attempts
        ):
            return b.intent.batch_id

    batch_id = new_batch_id(session.run_id, turn)
    intent = DeliveryBatchIntent(
        batch_id=batch_id,
        session_id=session.session_id,
        run_id=session.run_id,
        lane=lane_for_run(session.run_id),
        operation_id=operation_id_for_run(session.run_id),
        turn=turn,
        items=items,
        batch_digest=expected_digest,
    )
    delivery_store.record_batch_intent(session.session_id, session.run_id, intent)
    return batch_id


def build_next_tool_prompt(
    session: AgentLoopSession,
    turn_state: TurnState,
    *,
    protocol_reminder: str = "",
) -> str:
    """Build byte-exact next tool results prompt with coding context."""
    formatted = session.codec.format_results(turn_state.results)
    raw_prompt = f"{formatted}{protocol_reminder}" if protocol_reminder else formatted
    return append_coding_context(session, raw_prompt)


def deliver_turn_results(
    session: AgentLoopSession,
    turn_state: TurnState,
    turn: int,
    *,
    protocol_reminder: str = "",
) -> str:
    """Deliver a turn's tool results to the provider with durable delivery receipts."""
    batch_id = ensure_result_batch_intent(session, turn_state, turn)
    next_prompt = build_next_tool_prompt(
        session,
        turn_state,
        protocol_reminder=protocol_reminder,
    )
    return send_prompt(
        session,
        next_prompt,
        delivery_batch_id=batch_id,
        restart_request=(
            "Continue the unfinished task using the latest local tool results below.\n\n"
            f"{next_prompt}"
        ),
    )


def deliver_recovered_results(
    session: AgentLoopSession,
    turn_state: TurnState,
    *,
    recovered_batch_id: str = "",
) -> str:
    """Deliver recovered tool results on resume, marking delivery receipt on send."""
    next_prompt = build_next_tool_prompt(session, turn_state)
    return send_prompt(
        session,
        next_prompt,
        delivery_batch_id=recovered_batch_id,
        restart_request=(
            "Continue the unfinished task using the latest local tool results below.\n\n"
            f"{next_prompt}"
        ),
    )


__all__ = [
    "build_next_tool_prompt",
    "deliver_recovered_results",
    "deliver_turn_results",
    "ensure_result_batch_intent",
]
