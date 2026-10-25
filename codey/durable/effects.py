"""Pure effect receipts for an external outcome lost across process death."""

from dataclasses import replace

from codey.durable.model import EffectRecord, TaskCheckpoint


def interrupted_effect(task: TaskCheckpoint, effect: EffectRecord) -> EffectRecord:
    lane = effect.intent.get("lane")
    operation_id = effect.intent.get("operation_id")
    if not isinstance(lane, str) or not lane or not isinstance(operation_id, str) or not operation_id:
        raise ValueError("durable effect identity must be canonical before recovery")
    return replace(effect, status="interrupted", settlement={
        "schema_version": 1,
        "effect_kind": "runtime_effect",
        "record_kind": "settlement",
        "ref": f"effect_settlement:{effect.effect_id}",
        "effect_id": effect.effect_id,
        "effect_category": effect.category,
        "session_id": str(effect.intent.get("session_id") or task.conversation_id),
        "run_id": str(effect.intent.get("run_id") or task.task_id),
        "lane": lane,
        "operation_id": operation_id,
        "status": "interrupted",
        "error_code": "interrupted_by_crash",
        "sent_state": "maybe_sent" if effect.category == "provider_send" else "settled",
        "replay_class": effect.replay_class,
    })


def nested_effect_ids(effects: tuple[EffectRecord, ...]) -> set[str]:
    """Derive nested membership solely from committed effect intent links."""
    by_id = {effect.effect_id: effect for effect in effects}
    nested: set[str] = set()
    for effect in effects:
        parent_id = effect.intent.get("parent_effect_id", "")
        if not parent_id:
            continue
        parent = by_id.get(str(parent_id))
        if (parent is None or parent.intent.get("tool_name") != "codemode"
                or effect.effect_id == parent_id):
            raise ValueError("nested effect has no valid original parent")
        nested.add(effect.effect_id)
    return nested
