"""Read memory exposure and outcome facts from settled Core request receipts."""

from __future__ import annotations

from codey.durable.session import DurableSession
from codey.memory.codec import trace_from_payload
from codey.memory.context import candidate_item
from codey.operations.provider_receipts import REQUEST_KIND, read_provider_artifact, restore_context
from codey.storage.managed_outputs import ManagedOutputStore
from codey.types.json_value import JsonValue


def terminal_memory_facts(durable: DurableSession, outputs: ManagedOutputStore | None,
                          run_id: str) -> dict[str, JsonValue]:
    exposures: list[JsonValue] = []
    outcomes: list[JsonValue] = []
    task = durable.task(run_id)
    for effect in durable.settled_effects(run_id):
        settled = effect.settlement
        if settled is None:
            continue
        status = settled.get("status")
        if status not in {"ok", "error", "interrupted"}:
            status = "unknown"
        name = effect.intent.get("tool_name", "")
        outcomes.append({"effect_id": effect.effect_id, "category": effect.category,
                         "status": str(status), "tool_name": name if isinstance(name, str) else ""})
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
        _, sources = restore_context(request.get("context"))
        for source in sources:
            if not source.key.startswith("memory_"):
                continue
            item = candidate_item(source)
            exposures.append({"item_id": item.item_id, "source_ref": "request:" + artifact.handle})
    return {"exposures": exposures, "outcomes": outcomes}


def routing_for_run(durable: DurableSession, outputs: ManagedOutputStore, run_id: str) -> list[JsonValue]:
    """Return the frozen selection, without running today's retrieval policy."""
    task = durable.task(run_id)
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
                       "trace": [trace_from_payload(row).to_payload() for row in traces]})
    return result
