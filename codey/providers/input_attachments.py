"""Project admitted attachment data into the next actual provider request."""
from __future__ import annotations

from collections.abc import Mapping
from typing import Protocol

from codey.durable.model import thaw_json
from codey.durable.session import DurableSession
from codey.providers.context_ledger import ContextLedger
from codey.providers.tool_images import message_images
from codey.task.attachments import IMAGE_UNSUPPORTED, attachment_message, validate_attachments
from codey.types.json_value import decode_json_object, require_json_strings

ATTACHMENT_ENTRIES = "_codey_attachment_entries"


class AttachmentProvider(Protocol):
    @property
    def context_ledger(self) -> ContextLedger: ...
    @property
    def _durable(self) -> DurableSession | None: ...
    @property
    def _durable_source_entry_ids(self) -> tuple[str | None, ...]: ...
    @property
    def _durable_session_id(self) -> str: ...
    @property
    def _attachment_task_id(self) -> str: ...
    @property
    def supports_images(self) -> bool: ...
    @property
    def api_protocol(self) -> str: ...


def pending_attachments(durable: DurableSession | None, session_id: str, task_id: str, delivered: set[str]) -> tuple[list[dict[str, object]], list[str]]:
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
            if entry.by_task_id is None:
                continue
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


def with_attachment_content(message: dict[str, object], *, protocol: str, images: bool) -> dict[str, object]:
    if not images and message_images(message):
        raise ValueError(IMAGE_UNSUPPORTED)
    result = dict(message)
    attachments = result.pop("attachments", [])
    if attachments:
        result["content"] = attachment_message(str(result.get("content") or ""), validate_attachments(attachments), protocol=protocol, images=images)
    return result


def enrich_pending(provider: AttachmentProvider, pending: list[dict[str, object]]) -> list[dict[str, object]]:
    if not provider.supports_images and any(message_images(message) for message in pending):
        raise ValueError(IMAGE_UNSUPPORTED)
    events: object = provider.context_ledger.events()
    if not isinstance(events, list):
        raise TypeError("invalid provider context events")
    delivered = {identity for raw in events for event in (decode_json_object(raw),)
                 for identity in require_json_strings(event.get(ATTACHMENT_ENTRIES, []), "attachment entries")}
    delivered.update(identity for identity in provider._durable_source_entry_ids if identity)
    items, identities = pending_attachments(provider._durable, provider._durable_session_id, provider._attachment_task_id, delivered)
    if not items:
        return pending
    if not provider.supports_images and any(str(item["media_type"]).startswith("image/") for item in items):
        raise ValueError(IMAGE_UNSUPPORTED)
    result = [dict(item) for item in pending]
    target: dict[str, object] | None = next((item for item in reversed(result) if item.get("role") == "user"), None)
    if target is None or message_images(target):
        target = {"role": "user", "content": "Attached files"}
        result.append(target)
    target["content"] = attachment_message(str(target.get("content") or ""), items, protocol=provider.api_protocol, images=provider.supports_images)
    target[ATTACHMENT_ENTRIES] = identities
    return result
