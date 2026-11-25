"""Project optional metadata only from the accepted, delivered Core response."""

from __future__ import annotations

from typing import TYPE_CHECKING

from codey.durable.model import thaw_json
from codey.durable.session import DurableSession
from codey.memory.candidates import CANDIDATE_FIELD, candidate_from_payload
from codey.memory.contracts import MEMORY_CANDIDATE_LIMIT
from codey.operations.provider_receipts import REQUEST_KIND, RESPONSE_KIND, read_provider_artifact
from codey.protocols.text_turn import TextRequests, decode_text_turn
from codey.providers.base import AssistantTurn
from codey.providers.response_record import decode_reply
from codey.runtime.write.checkpoint import operation_from_checkpoint
from codey.storage.managed_outputs import ManagedOutputStore
from codey.types.json_value import JsonObject, JsonValue, decode_json_object

if TYPE_CHECKING:
    from codey.operations.kernel_contracts import DoneDecision


def accepted_done_decision(durable: DurableSession, outputs: ManagedOutputStore, run_id: str) -> tuple[DoneDecision | None, str]:
    """Read the last committed done decision; terminal checkpoints clear caches."""
    from codey.operations.kernel_control import validate_decision

    task = durable.task(run_id)
    for point in reversed(durable.task_timeline(run_id)):
        state = operation_from_checkpoint(point.task, task.conversation_id)
        if state is None or not state.work_ref:
            continue
        artifact = durable.artifact(state.work_ref)
        if artifact.owner_task_id != run_id or artifact.owner_effect_id is not None or artifact.metadata.get("kind") != "kernel_decision":
            raise ValueError("completion observation has an invalid decision owner")
        reference = decode_json_object(thaw_json(artifact.metadata.get("ref")))
        if reference.get("sha256") != artifact.sha256 or reference.get("handle") != artifact.handle:
            raise ValueError("completion observation digest reference differs")
        envelope = decode_json_object(outputs.read_json_artifact(dict[str, object](reference)))
        version = envelope.get("schema_version")
        if type(version) is not int or envelope.get("kind") != "kernel_decision":
            raise ValueError("invalid completion observation envelope")
        decision = validate_decision(envelope.get("data"), schema_version=version)
        if decision["purpose"] == "done":
            return decision, state.work_ref
    return None, ""


def accepted_completion(durable: DurableSession, outputs: ManagedOutputStore, run_id: str) -> tuple[JsonObject | None, str]:
    decision, ref = accepted_done_decision(durable, outputs, run_id)
    return (decode_json_object(decision["result"]) if decision is not None else None), ref


def accepted_response_ref(durable: DurableSession, outputs: ManagedOutputStore, run_id: str) -> str:
    """Resolve the response through the original accepted completion decision."""
    from codey.operations.kernel_control import validate_decision

    task = durable.task(run_id)
    if task.phase != "completed" or task.state.get("stop_reason") != "done":
        return ""
    for point in reversed(durable.task_timeline(run_id)):
        state = operation_from_checkpoint(point.task, task.conversation_id)
        if state is None or not state.work_ref or not state.response_ref:
            continue
        artifact = durable.artifact(state.work_ref)
        if artifact.owner_task_id != run_id or artifact.owner_effect_id is not None or artifact.metadata.get("kind") != "kernel_decision":
            raise ValueError("accepted memory response has an invalid decision owner")
        reference = decode_json_object(thaw_json(artifact.metadata.get("ref")))
        if reference.get("sha256") != artifact.sha256 or reference.get("handle") != artifact.handle:
            raise ValueError("accepted memory decision digest reference differs")
        envelope = decode_json_object(outputs.read_json_artifact(dict[str, object](reference)))
        if envelope.get("kind") != "kernel_decision":
            raise ValueError("invalid accepted memory decision")
        version = envelope.get("schema_version")
        if type(version) is not int:
            raise ValueError("invalid accepted decision version")
        decision = validate_decision(envelope.get("data"), schema_version=version)
        if decision["purpose"] == "done":
            response = durable.artifact(state.response_ref)
            effect = next((row for row in durable.effects(run_id) if row.effect_id == response.owner_effect_id), None)
            request_ref = effect.intent.get("request_ref") if effect is not None else None
            if effect is None or not isinstance(request_ref, str):
                continue
            request = read_provider_artifact(outputs, durable.artifact(request_ref), kind=REQUEST_KIND,
                session_id=task.conversation_id, run_id=run_id, effect_id=effect.effect_id)
            if request.get("close_reply") is True:
                continue
            return state.response_ref
    return ""


def completion_metadata(reply: str | AssistantTurn) -> JsonObject | None:
    if isinstance(reply, AssistantTurn):
        if len(reply.tool_calls) != 1 or reply.tool_calls[0].name != "done":
            return None
        return dict(reply.tool_calls[0].arguments)
    decoded = decode_text_turn(reply)
    if not isinstance(decoded, TextRequests) or len(decoded.items) != 1 or decoded.items[0].tool != "done":
        return None
    return dict(decoded.items[0].args)


def delivered_candidates(durable: DurableSession, outputs: ManagedOutputStore | None, *,
                         run_id: str, user_text: str, answer: str, scope: str, at: str) -> tuple[list[JsonValue], list[JsonValue]]:
    if outputs is None:
        return [], []
    task = durable.task(run_id)
    response_ref = accepted_response_ref(durable, outputs, run_id)
    if not response_ref:
        return [], []
    response = durable.artifact(response_ref)
    effect_id = response.owner_effect_id
    if effect_id is None:
        return [], []
    raw = read_provider_artifact(outputs, response, kind=RESPONSE_KIND, session_id=task.conversation_id,
                                 run_id=run_id, effect_id=effect_id)
    metadata = completion_metadata(decode_reply(raw))
    if metadata is None or metadata.get("summary") != answer or CANDIDATE_FIELD not in metadata:
        return [], []
    values = metadata[CANDIDATE_FIELD]
    if not isinstance(values, list) or len(values) > MEMORY_CANDIDATE_LIMIT:
        return [], [{"response_ref": response.handle, "reason": "invalid_candidate_collection"}]
    accepted: list[JsonValue] = []
    rejected: list[JsonValue] = []
    for index, value in enumerate(values):
        try:
            draft = candidate_from_payload(value, user_text=user_text, scope=scope, at=at)
        except ValueError as exc:
            rejected.append({"response_ref": response.handle, "index": index, "reason": str(exc)})
            continue
        accepted.append({"kind": draft.kind, "text": draft.text, "slot": draft.slot, "value": draft.value,
                         "context": draft.context, "expires_at": draft.expires_at,
                         "related_item_ids": list(draft.related_item_ids), "quote": draft.quote,
                         "quote_start": draft.quote_start, "quote_end": draft.quote_end,
                         "response_ref": response.handle, "index": index})
    return accepted, rejected
