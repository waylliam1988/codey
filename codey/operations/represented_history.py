"""Read exact retained history refs from the existing provider protocol view."""
from __future__ import annotations

from collections.abc import Callable

from codey.ghost.observation_index import history_records, history_refs
from codey.ghost.payload_types import object_fields
from codey.providers.compat_transport import provider_attribute
from codey.workspace.context_admission import GhostContextSource


def represented_history_refs(provider: object) -> frozenset[str]:
    """Use actual retained message bytes, not session identity or export summaries.

    Providers without the local protocol view simply keep retrieval. This is
    a read-only adapter to the existing window, not another context ledger.
    """
    messages = provider_attribute(provider, "_messages")
    if not isinstance(messages, list):
        return frozenset()
    entries: list[object] = messages
    refs: set[str] = set()
    for entry in entries:
        if not object_fields(entry):
            continue
        content = entry.get("content")
        if isinstance(content, str):
            refs.update(history_refs(content))
        elif isinstance(content, list):
            parts: list[object] = content
            for part in parts:
                if object_fields(part) and isinstance(text := part.get("text"), str):
                    refs.update(history_refs(text))
    return frozenset(refs)


def represented_inputs(provider: object, sources: tuple[GhostContextSource, ...],
                       input_entry_id: Callable[[str], str]) -> tuple[tuple[str, str], ...]:
    """Match an original user input by canonical entry ID AND complete bytes."""
    messages = provider_attribute(provider, "_messages")
    identities = provider_attribute(provider, "_durable_source_entry_ids")
    if not isinstance(messages, list) or not isinstance(identities, tuple) or len(messages) != len(identities):
        return ()
    entries: list[object] = messages
    ids: tuple[object, ...] = identities
    retained: list[tuple[str, str]] = []
    for source in sources:
        for row in history_records(source.text):
            run, part = row.get("run"), row.get("user")
            if not isinstance(run, str) or not object_fields(part):
                continue
            ref, text = part.get("ref"), part.get("text")
            if not isinstance(ref, str) or not isinstance(text, str):
                continue
            identity = input_entry_id(run)
            if not identity:
                continue
            for entry, entry_id in zip(entries, ids, strict=True):
                if entry_id == identity and object_fields(entry) and user_message_text(entry) == text:
                    retained.append((ref, text))
                    break
    return tuple(retained)


def user_message_text(entry: dict[str, object]) -> str | None:
    """Decode only an actual user message, never a tool description or summary."""
    if entry.get("role") != "user":
        return None
    content = entry.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list) and len(content) == 1:
        parts: list[object] = content
        part = parts[0]
        if (object_fields(part) and part.get("type") in {"text", "input_text"}
                and isinstance(text := part.get("text"), str)):
            return text
    return None
