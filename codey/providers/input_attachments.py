"""Project admitted attachment data into the next actual provider request."""
from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from codey.durable.model import thaw_json
from codey.task.attachments import IMAGE_UNSUPPORTED, attachment_message, validate_attachments

ATTACHMENT_ENTRIES = "_codey_attachment_entries"


def pending_attachments(durable: Any, session_id: str, task_id: str, delivered: set[str]) -> tuple[list[dict[str, object]], list[str]]:
    if durable is None or not task_id:
        return [], []
    task = durable.task(task_id)
    if task.conversation_id != session_id:
        raise ValueError("Attachment input belongs to another chat.")
    start = task.state.get("input_entry_id")
    entries = durable.context(session_id)
    index = next((i for i, entry in enumerate(entries) if entry.entry_id == start), None)
    if index is None:
        return [], []
    result: list[dict[str, object]] = []
    identities: list[str] = []
    for entry in entries[index:]:
        if entry.kind != "user_message" or entry.entry_id in delivered:
            continue
        if entry.by_task_id != task_id:
            try:
                owner = durable.task(entry.by_task_id)
            except KeyError:
                continue
            if owner.state.get("consumed_by_task_id") != task_id:
                continue
        payload = thaw_json(entry.payload)
        if isinstance(payload, Mapping) and payload.get("attachments"):
            result.extend(validate_attachments(payload["attachments"]))
            identities.append(entry.entry_id)
    return result, identities


def browser_attachment_text(provider_id: str, text: str, items: list[dict[str, object]]) -> str:
    from codey.providers.capabilities import capability_for
    from codey.task.attachments import attachment_text
    from codey.utils.text_budget import estimate_tokens

    if any(str(item["media_type"]).startswith("image/") for item in items):
        raise ValueError(IMAGE_UNSUPPORTED)
    result = text + attachment_text(items)
    capability = capability_for(provider_id)
    if items and estimate_tokens(result) > capability.context_window_tokens - capability.context_reserve_tokens:
        raise ValueError("Attached file content exceeds this model's input budget. Choose smaller files.")
    return result


def with_attachment_content(message: dict[str, Any], *, protocol: str, images: bool) -> dict[str, Any]:
    result = dict(message)
    attachments = result.pop("attachments", [])
    if attachments:
        result["content"] = attachment_message(str(result.get("content") or ""), validate_attachments(attachments), protocol=protocol, images=images)
    return result


def enrich_pending(provider: Any, pending: list[dict[str, Any]]) -> list[dict[str, Any]]:
    delivered = {identity for event in provider.context_ledger.events() for identity in event.get(ATTACHMENT_ENTRIES, [])}
    delivered.update(identity for identity in provider._durable_source_entry_ids if identity)
    items, identities = pending_attachments(provider._durable, provider._durable_session_id, provider._attachment_task_id, delivered)
    if not items:
        return pending
    if not provider.supports_images and any(str(item["media_type"]).startswith("image/") for item in items):
        raise ValueError(IMAGE_UNSUPPORTED)
    result = [dict(item) for item in pending]
    target: dict[str, object] | None = next((item for item in reversed(result) if item.get("role") == "user"), None)
    if target is None:
        target = {"role": "user", "content": "Attached files"}
        result.append(target)
    target["content"] = attachment_message(str(target.get("content") or ""), items, protocol=provider.api_protocol, images=provider.supports_images)
    target[ATTACHMENT_ENTRIES] = identities
    return result
