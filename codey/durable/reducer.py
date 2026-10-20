"""Pure checkpoint transitions shared by task and runtime transaction owners."""

from dataclasses import replace

from codey.durable.errors import CommitConflict
from codey.durable.model import EffectRecord, TaskCheckpoint, thaw_json


def with_effect_intents(current: TaskCheckpoint, effects: tuple[EffectRecord, ...]) -> TaskCheckpoint:
    if not effects or any(effect.task_id != current.task_id or effect.status != "pending"
                          or effect.settlement is not None for effect in effects):
        raise ValueError("effect intents must be pending and belong to the task")
    state = thaw_json(current.state)
    if not isinstance(state, dict):
        raise TypeError("task checkpoint state must be a JSON object")
    pending = list(state.get("pending_effect_ids", []))
    for effect in effects:
        if effect.effect_id not in pending:
            pending.append(effect.effect_id)
    categories = {effect.category for effect in effects}
    if categories == {"provider_send"} and current.phase in {
        "provider_prepared", "assistant_response_pending", "tool_outcome_ready",
    }:
        phase = "assistant_intent_committed"
    elif categories in ({"tool_call"}, {"shell"}) and current.phase in {
        # A provider generation may be settled at the post-tools boundary.
        # Its response can therefore admit tool effects while the provider
        # send receipt is still pending.  The task checkpoint owns both
        # intents until that boundary transaction closes the generation.
        "assistant_intent_committed", "assistant_response_pending",
        "tool_intent_committed", "tool_effect_running",
    }:
        phase = "tool_effect_running"
    elif len(categories) != 1:
        raise ValueError("an atomic effect batch must contain one effect category")
    else:
        raise CommitConflict(f"task phase {current.phase} does not permit these effect intents")
    return replace(current, phase=phase, revision=current.revision + 1,
                   state={**state, "pending_effect_ids": pending}, pending_effect_id=pending[-1])


def with_effect_settlement(current: TaskCheckpoint, effect: EffectRecord) -> TaskCheckpoint:
    if effect.task_id != current.task_id or effect.settlement is None or effect.status == "pending":
        raise ValueError("effect settlement requires a terminal outcome belonging to the task")
    state = thaw_json(current.state)
    if not isinstance(state, dict):
        raise TypeError("task checkpoint state must be a JSON object")
    pending = [item for item in state.get("pending_effect_ids", []) if item != effect.effect_id]
    phase = current.phase
    next_state = dict(state)
    if effect.category == "provider_send" and phase in {
        "assistant_intent_committed", "tool_effect_running",
    }:
        sent_state = effect.settlement.get("sent_state")
        if phase == "tool_effect_running" and pending:
            # Provider settlement may be committed at the post-tools
            # boundary while one or more tool effects from that generation
            # are still running. Keep the tool lifecycle phase until those
            # effects settle; jumping to an assistant phase is an illegal
            # transition and loses the remaining effect fence.
            next_state["provider_settlement_state"] = (
                "maybe_sent" if effect.status == "interrupted" or sent_state == "maybe_sent"
                else "not_sent" if sent_state == "not_sent" else "settled"
            )
            phase = "tool_effect_running"
        elif effect.status == "interrupted" or sent_state == "maybe_sent":
            # If tool effects from the same generation are still pending, the
            # checkpoint must keep their execution phase so their settlements
            # can commit. Once those effects are drained, retain the unknown
            # provider outcome in the explicit effect_unknown phase; jumping
            # to assistant_response_unknown would violate the tool lifecycle.
            if phase == "tool_effect_running":
                phase = "tool_effect_running" if any(pending) else "effect_unknown"
            else:
                phase = "assistant_response_unknown"
        elif sent_state == "not_sent":
            phase = "provider_prepared"
        elif phase == "tool_effect_running":
            # The tool receipt may be folded into the same transaction before
            # its deferred provider settlement. Once no tool fence remains,
            # the settled generation is ready for the next provider request.
            phase = "tool_outcome_ready"
        else:
            phase = "assistant_response_pending"
    elif effect.category in {"tool_call", "shell"} and phase == "tool_effect_running":
        if pending:
            phase = "tool_effect_running"
        else:
            provider_state = next_state.pop("provider_settlement_state", "settled")
            phase = "effect_unknown" if provider_state == "maybe_sent" else "tool_outcome_ready"
    return replace(current, phase=phase, revision=current.revision + 1,
                   state={**next_state, "pending_effect_ids": pending},
                   pending_effect_id=pending[-1] if pending else None)
