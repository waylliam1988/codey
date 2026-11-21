"""Physical attempt observations beside, never inside, frozen requests."""

from __future__ import annotations

from codey.durable.model import thaw_json
from codey.durable.session import DurableSession
from codey.memory.codec import digest
from codey.storage.managed_outputs import ManagedOutputStore
from codey.types.json_value import JsonObject, JsonValue, decode_json_object
from codey.workspace.context_source import ContextCandidate

KIND = "memory_attempt_admission"


def _positions(value: JsonValue, text: str, path: str = "") -> list[JsonValue]:
    result: list[JsonValue] = []
    if isinstance(value, str):
        start = value.find(text)
        if start >= 0:
            result.append({"path": path, "start": start, "end": start + len(text), "unit": "unicode_characters"})
    elif isinstance(value, list):
        for index, item in enumerate(value):
            result.extend(_positions(item, text, path + "/" + str(index)))
    elif isinstance(value, dict):
        for key, item in value.items():
            result.extend(_positions(item, text, path + "/" + key.replace("~", "~0").replace("/", "~1")))
    return result


def record_memory_attempt(durable: DurableSession, outputs: ManagedOutputStore, *, session_id: str,
                          run_id: str, sources: tuple[ContextCandidate, ...], payload: JsonObject, reason: str) -> None:
    task = durable.task(run_id)
    if task.conversation_id != session_id or not task.pending_effect_id:
        return
    effect = next((row for row in durable.effects(run_id) if row.effect_id == task.pending_effect_id), None)
    reference = effect.intent.get("request_ref") if effect is not None else None
    if effect is None or effect.category != "provider_send" or not isinstance(reference, str):
        return
    request = durable.artifact(reference)
    placements: list[JsonValue] = [{"source_key": source.key, "scope": source.scope,
        "source_refs": list(source.source_refs), "positions": _positions(payload, source.text)} for source in sources]
    observation: JsonObject = {"schema_version": 1, "request_ref": request.handle, "effect_id": effect.effect_id,
        "status": "admitted" if not reason else "rejected", "reason": reason, "payload_digest": digest(payload), "placements": placements}
    reference = outputs.write_json_artifact(session_id=session_id, run_id=run_id, payload=observation)
    artifact = outputs.json_artifact_record(reference, owner_task_id=run_id, owner_effect_id=effect.effect_id,
                                            metadata={"kind": KIND})
    durable.register_artifacts(run_id, (artifact,))


def read_memory_attempts(durable: DurableSession, outputs: ManagedOutputStore, effect_id: str,
                         *, run_id: str, request_ref: str) -> list[JsonValue]:
    records: list[JsonValue] = []
    for artifact in durable.artifacts_for_effect(effect_id):
        if artifact.metadata.get("kind") != KIND:
            continue
        if artifact.owner_task_id != run_id:
            raise ValueError("memory attempt observation has a different owner")
        ref = decode_json_object(thaw_json(artifact.metadata.get("ref")))
        if ref.get("sha256") != artifact.sha256 or ref.get("handle") != artifact.handle:
            raise ValueError("memory attempt observation digest reference differs")
        observation = decode_json_object(outputs.read_json_artifact(dict[str, object](ref)))
        if observation.get("request_ref") != request_ref or observation.get("effect_id") != effect_id:
            raise ValueError("memory attempt observation belongs to a different request")
        records.append(observation)
    return records
