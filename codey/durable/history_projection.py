from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import replace

from codey.durable.model import ArtifactRecord, EntryDraft, thaw_json
from codey.durable.session_kernel import SessionKernel
from codey.types.json_value import decode_json_object
from codey.types.protocol_window import ANTHROPIC_BLOCKS, REPLAY_BINDING, RESPONSES_REASONING, ProtocolWindowRef
from codey.utils.object_fields import object_fields


def _resolve_response_entries(
    session: SessionKernel, path: tuple[EntryDraft, ...],
    *, artifact: Callable[[str], ArtifactRecord] | None = None,
) -> tuple[EntryDraft, ...]:
    from codey.durable.provider_response import read_response_artifact, tool_call_message

    resolved = []
    for entry in path:
        reference = entry.payload.get("response_ref") if entry.kind == "tool_call" else None
        if reference is None:
            resolved.append(entry)
            continue
        if not isinstance(reference, str) or not reference or set(entry.payload) != {"response_ref"}:
            raise ValueError("invalid provider history reference")
        record = (artifact or session.store.artifact)(reference)
        if (record.owner_task_id != entry.by_task_id or not record.owner_effect_id
                or record.metadata.get("kind") != "provider_response"):
            raise ValueError("provider history reference ownership mismatch")
        envelope = read_response_artifact(record)
        if (not object_fields(envelope) or envelope.get("schema_version") != 1
                or envelope.get("kind") != "provider_response"
                or envelope.get("effect_id") != record.owner_effect_id):
            raise ValueError("invalid provider history response")
        payload = tool_call_message(envelope.get("payload"))
        if {"reasoning_content", RESPONSES_REASONING, ANTHROPIC_BLOCKS} & payload.keys():
            window = _original_provider_window(session, record, artifact or session.store.artifact)
            if window is not None:
                payload[REPLAY_BINDING] = window.binding.to_payload()
        resolved.append(replace(entry, payload=payload))
    return tuple(resolved)


def _original_provider_window(session: SessionKernel, response: ArtifactRecord,
                              artifact: Callable[[str], ArtifactRecord]) -> ProtocolWindowRef | None:
    from codey.durable.provider_response import read_response_artifact

    effect_id = response.owner_effect_id
    assert effect_id is not None
    try:
        effect = session.store.effect(effect_id)
        reference = effect.intent.get("request_ref")
        if effect.category != "provider_send" or effect.task_id != response.owner_task_id or not isinstance(reference, str) or not reference:
            return None
        request = artifact(reference)
        if request.owner_task_id != effect.task_id or request.metadata.get("kind") != "provider_request":
            raise ValueError("provider history request ownership mismatch")
        envelope = decode_json_object(read_response_artifact(request))
    except (KeyError, FileNotFoundError):
        return None
    if envelope.get("effect_id") != effect_id or envelope.get("kind") != "provider_request":
        raise ValueError("invalid provider history request")
    payload = decode_json_object(envelope.get("payload"))
    raw = payload.get("window")
    return ProtocolWindowRef.from_payload(raw) if raw is not None else None


def _history_item(entry: EntryDraft, ordinal: int) -> dict[str, object]:
    raw = thaw_json(entry.payload)
    if not object_fields(raw):
        raise ValueError("invalid durable history item")
    item = {key: value for key, value in raw.items() if key not in {REPLAY_BINDING, RESPONSES_REASONING, ANTHROPIC_BLOCKS}}
    if item.get("attachments"):
        from codey.task.attachments import public_attachment

        attachments = item["attachments"]
        if not isinstance(attachments, list) or any(not isinstance(ref, Mapping) for ref in attachments):
            raise ValueError("invalid durable attachments")
        item["attachments"] = [public_attachment(ref) for ref in attachments if isinstance(ref, Mapping)]
    if entry.kind == "control_review":
        raw_review = item.get("data")
        review: Mapping[str, object] = raw_review if object_fields(raw_review) else {}
        verdict = "Review passed" if review.get("passed") else "Changes requested"
        item.update(type="info", text=f"{review.get('source', 'Reviewer')} · {verdict}: {review.get('summary', '')}")
    item.setdefault("type", {
        "user_message": "user", "assistant_message": "asst", "tool_call": "tool_call",
        "tool_result": "tool_result", "compaction": "info",
    }.get(entry.kind, entry.kind))
    item["id"] = entry.entry_id
    item["entry_id"] = entry.entry_id
    item["ordinal"] = ordinal
    if entry.by_task_id is not None:
        item["runId"] = entry.by_task_id
        if entry.kind == "assistant_message":
            item["finalAnswer"] = True
            item["contentRole"] = "final"
    return item
