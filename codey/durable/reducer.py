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
        "assistant_response_pending", "tool_intent_committed", "tool_effect_running",
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
    if effect.category == "provider_send" and phase == "assistant_intent_committed":
        sent_state = effect.settlement.get("sent_state")
        if effect.status == "interrupted" or sent_state == "maybe_sent":
            phase = "assistant_response_unknown"
        elif sent_state == "not_sent":
            phase = "provider_prepared"
        else:
            phase = "assistant_response_pending"
    elif effect.category in {"tool_call", "shell"} and phase == "tool_effect_running":
        phase = "tool_effect_running" if pending else "tool_outcome_ready"
    return replace(current, phase=phase, revision=current.revision + 1,
                   state={**state, "pending_effect_ids": pending},
                   pending_effect_id=pending[-1] if pending else None)
