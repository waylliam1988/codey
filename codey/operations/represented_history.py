"""Read exact retained history refs from the existing provider protocol view."""
from __future__ import annotations

import json
from collections.abc import Callable

from codey.ghost.observation_index import history_records, history_refs
from codey.ghost.payload_types import object_fields
from codey.providers.compat_transport import provider_attribute
from codey.providers.context_checkpoint import summary_source
from codey.types.json_value import decode_json_value
from codey.workspace.context_admission import GhostContextSource


def represented_history_refs(provider: object) -> frozenset[str]:
    """Use actual retained message bytes, not session identity or export summaries.

    Providers without the local protocol view simply keep retrieval. This is
    a read-only adapter to the existing window, not another context ledger.
    """
    return frozenset(ref for text in _protocol_texts(provider) for ref in history_refs(text))


def _protocol_texts(provider: object) -> tuple[str, ...]:
    messages = provider_attribute(provider, "_messages")
    if not isinstance(messages, list):
        return ()
    entries: list[object] = messages
    texts: list[str] = []
    for entry in entries:
        if not object_fields(entry):
            continue
        content = entry.get("content")
        if isinstance(content, str):
            texts.append(content)
        elif isinstance(content, list):
            parts: list[object] = content
            for part in parts:
                if object_fields(part) and isinstance(text := part.get("text"), str):
                    texts.append(text)
    return tuple(texts)


def compaction_history_refs(payload: dict[str, object], provider: object,
                            inputs: tuple[tuple[str, str], ...]) -> frozenset[str]:
    """Recognize complete source JSON, including the production lossless codec.

    Encoded text must equal the existing encoder's output for retained bytes.
    This avoids a second codec and unbounded expansion of model-facing data.
    It does not confer authority on summaries or partial source fragments.
    """
    originals = _protocol_texts(provider)
    encodings = tuple((summary_source(text), text) for text in originals if history_refs(text))
    refs: set[str] = set()
    for key in ("messages", "input"):
        messages = payload.get(key)
        if not isinstance(messages, list):
            continue
        entries: list[object] = messages
        for entry in entries:
            prompt = user_message_text(entry) if object_fields(entry) else None
            if prompt is None or not prompt.startswith("<source>\n"):
                continue
            raw, closing, _instruction = prompt[len("<source>\n"):].rpartition("\n</source>\n")
            if not closing:
                continue
            try:
                decoded: object = json.loads(raw)
                source = decode_json_value(decoded)
            except (ValueError, RecursionError):
                continue
            if not isinstance(source, list):
                continue
            for message in source:
                if not isinstance(message, dict):
                    continue
                content = message.get("content")
                values = [part.get("text") for part in content if isinstance(part, dict)
                          and part.get("type") in {"text", "input_text"}] if isinstance(content, list) else [content]
                for value in values:
                    if isinstance(value, str):
                        refs.update(history_refs(value))
                    else:
                        for encoded, text in encodings:
                            if value == encoded:
                                refs.update(history_refs(text))
                user_content = _user_message_content(dict[str, object](message))
                refs.update(ref for ref, text in inputs
                            if user_content == text or user_content == summary_source(text))
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
    content = _user_message_content(entry)
    return content if isinstance(content, str) else None


def _user_message_content(entry: dict[str, object]) -> object:
    if entry.get("role") != "user":
        return None
    content = entry.get("content")
    if not isinstance(content, list):
        return content
    if len(content) == 1:
        parts: list[object] = content
        part = parts[0]
        if object_fields(part) and part.get("type") in {"text", "input_text"}:
            return part.get("text")
    return None
