"""Pure recovery decisions from the canonical task checkpoint and effects."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from codey.durable.model import EffectRecord, TaskCheckpoint

RecoveryAction = Literal[
    "continue",
    "settle_provider_unknown",
    "settle_tool_unknown",
    "recover_tool_effects",
    "fail_invariant",
]


@dataclass(frozen=True)
class RecoveryDecision:
    action: RecoveryAction
    effect_ids: tuple[str, ...] = ()
    reason: str = ""


def decide_recovery(
    task: TaskCheckpoint,
    effects: tuple[EffectRecord, ...],
) -> RecoveryDecision:
    pending = tuple(effect for effect in effects if effect.settlement is None)
    pending_ids = tuple(effect.effect_id for effect in pending)
    if task.phase in {"completed", "failed", "cancelled"}:
        return RecoveryDecision("continue") if not pending else RecoveryDecision(
            "fail_invariant", pending_ids, "terminal task retains pending effects"
        )
    if task.phase == "assistant_intent_committed":
        providers = tuple(
            effect for effect in pending if effect.category == "provider_send"
        )
        if len(providers) == 1 and len(pending) == 1:
            return RecoveryDecision("settle_provider_unknown", pending_ids)
        return RecoveryDecision("fail_invariant", pending_ids, "provider checkpoint/effects mismatch")
    if task.phase == "assistant_response_unknown":
        # The provider may already have received the request.  A settled
        # ``maybe_sent`` receipt is still an unknown external outcome and must
        # never be treated as an ordinary continuation.  Recovery will seal
        # the task for explicit operator handling; it must not submit again.
        unknown = tuple(
            effect.effect_id
            for effect in effects
            if effect.category == "provider_send"
            and isinstance(effect.settlement, dict)
            and effect.settlement.get("sent_state") == "maybe_sent"
        )
        if unknown:
            return RecoveryDecision(
                "fail_invariant", unknown,
                "provider response outcome remains unknown; request was not replayed",
            )
        return RecoveryDecision(
            "fail_invariant", pending_ids,
            "provider response checkpoint has no authoritative settlement",
        )
    if task.phase == "tool_effect_running":
        tools = tuple(effect for effect in pending if effect.category == "tool_call")
        if tools and len(tools) == len(pending):
            # The durable intent already carries the replay classification.
            # Unsafe effects must never be handed back to a projection or
            # runtime reducer that could accidentally execute them.
            if all(effect.replay_class == "safe" for effect in tools):
                return RecoveryDecision("recover_tool_effects", pending_ids)
            return RecoveryDecision(
                "settle_tool_unknown",
                pending_ids,
                "tool effect outcome was unknown after process restart",
            )
        return RecoveryDecision("fail_invariant", pending_ids, "tool checkpoint/effects mismatch")
    if pending:
        return RecoveryDecision("fail_invariant", pending_ids, "unexpected pending effect")
    return RecoveryDecision("continue")


__all__ = ["RecoveryAction", "RecoveryDecision", "decide_recovery"]
