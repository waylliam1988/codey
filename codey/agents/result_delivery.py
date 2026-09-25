"""Tool result delivery coordinator and prompt dispatch.

Unifies the formatting, context injection, durable delivery receipt recording,
and outbound provider transmission of tool results.

Does not import operations, ghost, or web providers.
"""

from __future__ import annotations

from codey.agents.prompt_context import (
    append_coding_context,
    provider_supports_structured,
    send_prompt,
    send_structured_results,
)
from codey.agents.state import AgentLoopSession
from codey.agents.tool_execution import TurnState
from codey.runtime.effects.tool_result_delivery import (
    DeliveryBatchIntent,
    DeliveryBatchItem,
    ToolResultDeliveryError,
    compute_batch_digest,
    new_batch_id,
)


def ensure_result_batch_intent(
    session: AgentLoopSession,
    turn_state: TurnState,
    turn: int,
) -> str:
    """Find existing matching batch intent or record durable batch intent (fail-closed)."""
    if not turn_state.delivery_items:
        return ""
    delivery_store = session.request.tool_result_delivery
    mutations = session.request.runtime_mutations
    if (
        delivery_store is None
        and mutations is None
        and not session.request.session_id
        and not session.request.run_id
    ):
        # Ephemeral in-memory runs (unit tests, ad-hoc FakeProvider loops):
        # no ids and no sinks, so there is nothing durable to preserve.
        return ""
    if delivery_store is None:
        raise ToolResultDeliveryError(
            f"turn {turn} has delivery items but no durable delivery store"
        )
    if not session.request.session_id or not session.request.run_id:
        raise ToolResultDeliveryError(
            f"turn {turn} has delivery items but missing session_id/run_id"
        )

    raw_items: list[DeliveryBatchItem] = []
    for item in turn_state.delivery_items:
        if not item.ref or not isinstance(item.ref, str) or not item.ref.strip():
            raise ToolResultDeliveryError(
                f"missing canonical ref in delivery item for tool {item.tool_name!r}"
            )
        raw_items.append(
            DeliveryBatchItem(
                tool_index=item.tool_index,
                tool_name=item.tool_name,
                ref=item.ref,
                replay_class=item.replay_class,
                is_denied=item.is_denied,
            )
        )
    items = tuple(raw_items)
    expected_digest = compute_batch_digest(items)

    # Fast path: turn_state already recorded the batch intent at the start of this turn
    if turn_state.delivery_batch_id:
        if turn_state.delivery_batch_digest == expected_digest:
            return turn_state.delivery_batch_id
        raise ToolResultDeliveryError(
            f"turn {turn} delivery batch envelope mismatch: expected digest {expected_digest!r}, "
            f"found early batch {turn_state.delivery_batch_id!r} with digest {turn_state.delivery_batch_digest!r}"
        )

    # Slow / failover path: inspect durable store if not present in turn_state
    batches = delivery_store.load_batches(session.request.session_id, session.request.run_id)
    for b in reversed(batches):
        if b.intent.turn == turn and not b.is_delivered and not b.send_attempts:
            if b.intent.batch_digest == expected_digest:
                return b.intent.batch_id
            # Invariant failure: an early planned batch for this exact turn exists without any send attempts,
            # but its envelope digest does not match the results about to be delivered.
            raise ToolResultDeliveryError(
                f"turn {turn} delivery batch envelope mismatch: expected digest {expected_digest!r}, "
                f"found early batch {b.intent.batch_id!r} with digest {b.intent.batch_digest!r}"
            )
            # Note: if a batch for this turn already has send_attempts, it indicates a prior
            # provider attempt in this turn that failed or timed out. A new writer/failover run
            # may record a fresh batch for recovery tracking.

    if mutations is None:
        raise ToolResultDeliveryError(
            f"turn {turn} has delivery items but no runtime mutations sink"
        )
    batch_id = new_batch_id(session.request.run_id, turn)
    intent = DeliveryBatchIntent(
        batch_id=batch_id,
        session_id=session.request.session_id,
        run_id=session.request.run_id,
        turn=turn,
        items=items,
        batch_digest=expected_digest,
    )
    mutations.begin_tool_batch(
        session.request.session_id,
        session.request.run_id,
        intents=(),
        delivery_intent=intent,
    )
    turn_state.delivery_batch_id = batch_id
    turn_state.delivery_batch_digest = expected_digest
    return batch_id


def build_next_tool_prompt(
    session: AgentLoopSession,
    turn_state: TurnState,
    *,
    protocol_reminder: str = "",
) -> str:
    """Build byte-exact next tool results prompt with coding context."""
    formatted = session.config.codec.format_results(turn_state.results)
    raw_prompt = f"{formatted}{protocol_reminder}" if protocol_reminder else formatted
    return append_coding_context(session, raw_prompt)


def _use_native_delivery(session: AgentLoopSession) -> bool:
    return session.config.native_tools is not None and provider_supports_structured(session)


def deliver_turn_results(
    session: AgentLoopSession,
    turn_state: TurnState,
    turn: int,
    *,
    protocol_reminder: str = "",
) -> str | object:
    """Deliver a turn's tool results to the provider with durable delivery receipts."""
    batch_id = ensure_result_batch_intent(session, turn_state, turn)
    if _use_native_delivery(session):
        from codey.protocols.native_openai import NativeOpenAIToolCodec

        tool_messages = NativeOpenAIToolCodec.tool_messages(turn_state.results)
        if tool_messages:
            # fallback_text stays lazy: building it eagerly would run
            # append_coding_context and pollute pending_context_rows that a
            # native role:tool send never binds.
            def _fallback_text() -> str:
                return build_next_tool_prompt(
                    session,
                    turn_state,
                    protocol_reminder=protocol_reminder,
                )

            return send_structured_results(
                session,
                tool_messages,
                restart_request="Continue the unfinished task using the latest local tool results.",
                delivery_batch_id=batch_id,
                overflow_fallback_prompt=(
                    protocol_reminder or "Continue from these results; reply with the next tool call."
                ),
                fallback_text=_fallback_text,
            )
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
    turn: int,
    recovered_batch_id: str = "",
) -> str | object:
    """Deliver recovered tool results on resume, marking delivery receipt on send."""
    batch_id = recovered_batch_id or ensure_result_batch_intent(session, turn_state, turn)
    if _use_native_delivery(session):
        from codey.protocols.native_openai import NativeOpenAIToolCodec, NativeToolResultError

        try:
            tool_messages = NativeOpenAIToolCodec.tool_messages(turn_state.results)
        except NativeToolResultError:
            # Recovered calls predate native call_ids; fall back to text delivery.
            tool_messages = []
        if tool_messages:
            def _recovered_fallback_text() -> str:
                return build_next_tool_prompt(session, turn_state)

            return send_structured_results(
                session,
                tool_messages,
                restart_request="Continue the unfinished task using the latest local tool results.",
                delivery_batch_id=batch_id,
                overflow_fallback_prompt="Continue from these results; reply with the next tool call.",
                fallback_text=_recovered_fallback_text,
            )
    next_prompt = build_next_tool_prompt(session, turn_state)
    return send_prompt(
        session,
        next_prompt,
        delivery_batch_id=batch_id,
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
