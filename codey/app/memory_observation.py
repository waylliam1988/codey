"""Read memory exposure and outcome facts from settled Core request receipts."""

from __future__ import annotations

from codey.app.memory_behaviors import answer_behaviors, tool_outcome
from codey.durable.session import DurableSession
from codey.memory.codec import digest, trace_from_payload
from codey.memory.context import candidate_item
from codey.memory.contracts import MemoryExposure
from codey.operations.provider_receipts import REQUEST_KIND, read_provider_artifact, restore_context
from codey.storage.managed_outputs import ManagedOutputStore
from codey.types.json_value import JsonValue


def terminal_memory_facts(durable: DurableSession, outputs: ManagedOutputStore | None,
                          run_id: str) -> dict[str, JsonValue]:
    exposures: list[JsonValue] = []
    outcomes: list[JsonValue] = []
    from codey.app.memory_verification import completion_observations

    outcomes.extend(completion_observations(durable, outputs, run_id))
    task = durable.task(run_id)
    last_request = ""
    for effect in durable.settled_effects(run_id):
        settled = effect.settlement
        if settled is None:
            continue
        status = settled.get("status")
        if status not in {"ok", "error", "interrupted"}:
            status = "unknown"
        name = effect.intent.get("tool_name", "")
        outcome: JsonValue = {"effect_id": effect.effect_id, "category": effect.category,
                             "status": str(status), "tool_name": name if isinstance(name, str) else ""}
        if effect.category == "tool_call" and settled.get("result_payload_sha256"):
            try:
                outcome = tool_outcome(effect, outputs, session_id=task.conversation_id, run_id=run_id)
            except (ValueError, OSError):
                outcome = {"effect_id": effect.effect_id, "category": effect.category, "status": "unknown", "reason": "invalid_receipt"}
        outcomes.append(outcome)
        if outputs is None or effect.category != "provider_send" or status != "ok":
            continue
        request_ref = effect.intent.get("request_ref")
        if not isinstance(request_ref, str) or not request_ref:
            continue
        artifact = durable.artifact(request_ref)
        request = read_provider_artifact(outputs, artifact, kind=REQUEST_KIND,
            session_id=task.conversation_id, run_id=run_id, effect_id=effect.effect_id)
        if request.get("close_reply") is True:
            continue
        last_request = "request:" + artifact.handle
        _, sources = restore_context(request.get("context"))
        raw_traces = request.get("memory_traces", [])
        if not isinstance(raw_traces, list):
            raise ValueError("frozen memory traces must be an array")
        consumers = {trace.item_id: trace.consumer for value in raw_traces if (trace := trace_from_payload(value)).consumer}
        for source in sources:
            if not source.key.startswith("memory_"):
                continue
            item = candidate_item(source)
            version = digest(item.to_payload())
            identity = digest({"request": artifact.handle, "item": item.item_id, "version": version})
            context = consumers.get(item.item_id, item.context)
            exposures.append(MemoryExposure(identity, item.item_id, version, run_id,
                "request:" + artifact.handle, item.scope, "", context if isinstance(context, str) else "").to_payload())
    answer_id = durable.submission(task.submission_id).answer_entry_id
    answer = next((row for row in durable.context(task.conversation_id) if row.entry_id == answer_id), None)
    text = answer.payload.get("text") if answer is not None else ""
    behaviors = answer_behaviors(text, run_id=run_id, request_ref=last_request, observation_ref="answer:" + answer_id) if isinstance(text, str) and last_request and answer_id else []
    return {"exposures": exposures, "outcomes": outcomes, "behaviors": behaviors}


def routing_for_run(durable: DurableSession, outputs: ManagedOutputStore, run_id: str) -> list[JsonValue]:
    """Return the frozen selection, without running today's retrieval policy."""
    task = durable.task(run_id)
    from codey.operations.memory_routing import read_memory_attempts
    result: list[JsonValue] = []
    for effect in durable.effects(run_id):
        reference = effect.intent.get("request_ref")
        if effect.category != "provider_send" or not isinstance(reference, str) or not reference:
            continue
        artifact = durable.artifact(reference)
        request = read_provider_artifact(outputs, artifact, kind=REQUEST_KIND,
            session_id=task.conversation_id, run_id=run_id, effect_id=effect.effect_id)
        traces = request.get("memory_traces", [])
        if not isinstance(traces, list):
            raise ValueError("frozen memory routing is invalid")
        result.append({"effect_id": effect.effect_id, "status": effect.status,
                       "request_ref": artifact.handle,
                       "attempts": read_memory_attempts(durable, outputs, effect.effect_id, run_id=run_id, request_ref=artifact.handle),
                       "trace": [trace_from_payload(row).to_payload() for row in traces]})
    return result
