"""Observable answer features and lossless Core tool receipts for memory."""

from __future__ import annotations

import re

from codey.durable.model import EffectRecord
from codey.memory.codec import digest
from codey.memory.contracts import MemoryBehavior
from codey.operations.kernel_receipts import restore_result_receipt
from codey.runtime.effects.effect_records import RuntimeEffectIntent, RuntimeEffectProjection, RuntimeEffectSettlement
from codey.storage.managed_outputs import ManagedOutputStore
from codey.types.json_value import JsonObject, JsonValue


def answer_behaviors(answer: str, *, run_id: str, request_ref: str, observation_ref: str) -> list[JsonValue]:
    values: list[JsonValue] = []
    pattern: re.Pattern[str] = re.compile(r"```([A-Za-z0-9_+-]+)[ \t]*\r?\n")
    languages = {match.group()[3:].strip() for match in pattern.finditer(answer)}
    for language in sorted(languages):
        identity = digest({"run": run_id, "answer": observation_ref, "kind": "code_fence_language", "value": language})
        values.append(MemoryBehavior(identity, run_id, request_ref, "code_fence_language", language.casefold(), observation_ref).to_payload())
    return values


def tool_outcome(effect: EffectRecord, outputs: ManagedOutputStore | None, *, session_id: str,
                 run_id: str) -> JsonObject:
    settled = effect.settlement
    if settled is None:
        raise ValueError("tool outcome has no settlement")
    intent_payload: dict[str, object] = dict(effect.intent)
    settlement_payload: dict[str, object] = dict(settled)
    projection = RuntimeEffectProjection(RuntimeEffectIntent.from_payload(intent_payload),
                                         RuntimeEffectSettlement.from_payload(settlement_payload))
    receipt = restore_result_receipt(projection, store=outputs, session_id=session_id, run_id=effect.task_id)
    identity = receipt.canonical.get("workspace_identity")
    revision = identity.get("workspace_fingerprint", "") if isinstance(identity, dict) else ""
    command = receipt.call.args.get("command", "")
    return {"effect_id": effect.effect_id, "run_id": run_id, "category": effect.category,
            "at": projection.settlement.created_at if projection.settlement is not None else "",
            "tool_name": receipt.call.name, "status": "ok" if receipt.ok else "error",
            "result_ref": projection.settlement.result_ref if projection.settlement is not None else "",
            "action_digest": effect.args_digest, "command": command if isinstance(command, str) and len(command) <= 256 else "",
            "workspace_revision": revision if isinstance(revision, str) else "",
            "value": "unknown", "reason": "tool_success_is_not_verification"}
