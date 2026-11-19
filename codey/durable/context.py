"""Pure context construction from durable conversation entries."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, replace

from codey.durable.model import EntryDraft, thaw_json
from codey.utils.object_fields import mapping_fields, object_fields
from codey.utils.text_budget import estimate_tokens


@dataclass(frozen=True)
class ContextSnapshot:
    entry_ids: tuple[str, ...]
    messages: tuple[Mapping[str, object], ...]
    token_count: int
    digest: str
    omitted_entry_ids: tuple[str, ...]


def build_context(
    entries: Sequence[EntryDraft],
    *,
    token_budget: int,
    token_counter: Callable[[tuple[Mapping[str, object], ...]], int] | None = None,
) -> ContextSnapshot:
    if token_budget <= 0:
        raise ValueError("token_budget must be positive")
    # Memory management is audit data, never model history or context edits.
    entries = tuple(entry for entry in entries if entry.kind not in {"memory_proposal", "memory_decision"})
    selected: list[EntryDraft] = []
    latest_compaction = max(
        (index for index, entry in enumerate(entries) if entry.kind == "compaction"),
        default=-1,
    )
    window = entries[latest_compaction:] if latest_compaction >= 0 else entries
    window = _apply_context_edits(window)
    candidates = [
        entry
        for entry in window
        if entry.kind not in {"context_snapshot", "context_edit", "display_message"}
        and not _is_display_only(entry)
        and not _is_aborted_assistant(entry)
    ]
    for entry in reversed(candidates):
        tentative = [entry, *selected]
        cost = _count_messages(tentative, token_counter, candidates=candidates)
        if selected and cost > token_budget:
            break
        if not selected and cost > token_budget:
            if entry.kind not in {"user_message", "assistant_message"}:
                break
            truncated = _fit_text_entry(entry, token_budget, token_counter)
            if truncated is not None:
                selected.append(truncated)
            break
        selected.append(entry)
    selected.reverse()
    selected = _restore_tool_pairs(selected, candidates)
    messages = tuple(_message_for(entry) for entry in selected)
    token_count = _token_count(messages, token_counter)
    while selected and token_count > token_budget:
        _drop_oldest_context_unit(selected)
        selected = _restore_tool_pairs(selected, candidates)
        messages = tuple(_message_for(entry) for entry in selected)
        token_count = _token_count(messages, token_counter)
    encoded = json.dumps(messages, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    selected_sources = {_source_entry_id(entry.entry_id) for entry in selected}
    omitted = tuple(
        entry.entry_id
        for entry in entries
        if entry.entry_id not in selected_sources
        and entry.kind not in {"context_snapshot", "context_edit"}
    )
    return ContextSnapshot(
        entry_ids=tuple(entry.entry_id for entry in selected),
        messages=messages,
        token_count=token_count,
        digest=hashlib.sha256(encoded.encode("utf-8")).hexdigest(),
        omitted_entry_ids=omitted,
    )


def _message_for(entry: EntryDraft) -> Mapping[str, object]:
    raw_payload = thaw_json(entry.payload)
    payload = raw_payload if object_fields(raw_payload) else {}
    if entry.kind == "user_message":
        return {"role": "user", "content": str(payload.get("text") or ""),
                **({"attachments": payload["attachments"]} if payload.get("attachments") else {})}
    if entry.kind == "assistant_message":
        return {"role": "assistant", "content": str(payload.get("text") or "")}
    if entry.kind == "tool_call":
        assistant_message: dict[str, object] = {"role": "assistant", "tool_calls": payload.get("tool_calls", [payload])}
        for key in ("content", "reasoning_content"):
            if isinstance(payload.get(key), str):
                assistant_message[key] = payload[key]
        return assistant_message
    if entry.kind == "tool_result":
        message: dict[str, object] = {
            "role": "tool",
            "content": str(payload.get("content") or payload.get("text") or ""),
        }
        call_id = payload.get("call_id")
        if isinstance(call_id, str) and call_id:
            message["tool_call_id"] = call_id
        if payload.get("is_error") is True:
            message["is_error"] = True
        return message
    if entry.kind == "compaction":
        return {"role": "system", "content": _summary_text(payload.get("summary"))}
    return {"role": "system", "content": json.dumps(payload, ensure_ascii=False, sort_keys=True)}


def _is_aborted_assistant(entry: EntryDraft) -> bool:
    """Keep incomplete assistant attempts out of provider context.

    A streamed response is represented by an intent/start entry until its
    final message is committed.  If that final entry records an interrupted
    or error status, replaying its partial text as a successful assistant turn
    can cause the next provider request to accept a response that never
    completed.
    """
    if entry.kind != "assistant_message":
        return False
    payload = entry.payload
    status = payload.get("status")
    return isinstance(status, str) and status.casefold() in {
        "aborted",
        "error",
        "interrupted",
    }


def _is_display_only(entry: EntryDraft) -> bool:
    return entry.payload.get("display_only") is True


def _summary_text(value: object) -> str:
    if isinstance(value, str):
        return value
    if mapping_fields(value):
        summary = dict(value)
        return json.dumps(summary, ensure_ascii=False, sort_keys=True)
    return str(value or "")


def _estimate_tokens(messages: tuple[Mapping[str, object], ...]) -> int:
    if not messages:
        return 0
    from codey.task.attachments import attachment_message, image_estimate_view, validate_attachments

    value = [{**message, "attachments": list[dict[str, object]](), "content": attachment_message(str(message.get("content") or ""), validate_attachments(message["attachments"]), protocol="openai-completions", images=True)}
             if message.get("attachments") else dict(message) for message in messages]
    view, images = image_estimate_view(value)
    encoded = json.dumps(view, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return estimate_tokens(encoded) + images


def _token_count(
    messages: tuple[Mapping[str, object], ...],
    token_counter: Callable[[tuple[Mapping[str, object], ...]], int] | None,
) -> int:
    if not messages:
        return 0
    count = token_counter(messages) if token_counter is not None else _estimate_tokens(messages)
    if type(count) is not int or count < 0:
        raise ValueError("provider token counter must return a non-negative integer")
    return count


def _count_messages(
    entries: Sequence[EntryDraft],
    token_counter: Callable[[tuple[Mapping[str, object], ...]], int] | None,
    *,
    candidates: Sequence[EntryDraft] | None = None,
) -> int:
    if candidates is not None:
        entries = _restore_tool_pairs(list(entries), candidates)
    return _token_count(tuple(_message_for(entry) for entry in entries), token_counter)


def _fit_text_entry(
    entry: EntryDraft,
    token_budget: int,
    token_counter: Callable[[tuple[Mapping[str, object], ...]], int] | None,
) -> EntryDraft | None:
    """Keep a prefix that actually fits, including message framing costs."""
    text = str(_message_for(entry).get("content") or "")
    low, high = 1, len(text)
    best = None
    while low <= high:
        middle = (low + high) // 2
        candidate = replace(entry, payload={**dict(entry.payload), "text": text[:middle]})
        if _count_messages((candidate,), token_counter) <= token_budget:
            best = candidate
            low = middle + 1
        else:
            high = middle - 1
    return best


def _restore_tool_pairs(selected: list[EntryDraft], candidates: Sequence[EntryDraft]) -> list[EntryDraft]:
    selected_ids = {entry.entry_id for entry in selected}
    index = {entry.entry_id: position for position, entry in enumerate(candidates)}
    additions: list[EntryDraft] = []
    removals: set[str] = set()
    paired_calls: set[str] = set()
    paired_results: set[str] = set()

    # A selected call must remain a valid provider message even if the process
    # died before its result was committed.  Prefer a committed result, then
    # synthesize an explicit error result; silently dropping the call changes
    # the model's history and makes the next request non-deterministic.
    for entry in selected:
        if entry.kind != "tool_call":
            continue
        call_ids = _tool_call_ids(entry)
        for tool_call_id in call_ids:
            result = _find_tool_result(candidates, index[entry.entry_id], tool_call_id)
            if result is not None:
                paired_calls.add(entry.entry_id)
                paired_results.add(result.entry_id)
                if result.entry_id not in selected_ids:
                    additions.append(result)
                    selected_ids.add(result.entry_id)
                continue
            synthetic_id = f"{entry.entry_id}:missing-result:{tool_call_id}"
            if synthetic_id in selected_ids:
                paired_calls.add(entry.entry_id)
                paired_results.add(synthetic_id)
                continue
            additions.append(
                EntryDraft(
                    synthetic_id,
                    entry.conversation_id,
                    entry.entry_id,
                    "tool_result",
                    {
                        "call_id": tool_call_id,
                        "content": _missing_tool_result_text(entry, tool_call_id),
                        "is_error": True,
                        "synthetic": True,
                    },
                )
            )
            selected_ids.add(synthetic_id)
            paired_calls.add(entry.entry_id)
            paired_results.add(synthetic_id)

    for entry in selected:
        if entry.kind != "tool_result":
            continue
        if entry.entry_id not in index:
            if entry.entry_id in paired_results:
                continue
            removals.add(entry.entry_id)
            continue
        match = _find_tool_call(candidates, index[entry.entry_id], entry.payload.get("call_id"))
        if match is None:
            removals.add(entry.entry_id)
            continue
        paired_calls.add(match.entry_id)
        paired_results.add(entry.entry_id)
        if match.entry_id not in selected_ids:
            additions.append(match)
            selected_ids.add(match.entry_id)
    for entry in (*selected, *additions):
        if entry.kind == "tool_call" and entry.entry_id not in paired_calls:
            removals.add(entry.entry_id)
    for entry in selected:
        if entry.kind == "tool_result" and entry.entry_id not in paired_results:
            removals.add(entry.entry_id)
    def sort_position(entry: EntryDraft) -> tuple[int, bool]:
        return (index[entry.entry_id] if entry.entry_id in index else index[entry.parent_id or ""],
                entry.entry_id not in index)

    return sorted(
        (entry for entry in (*selected, *additions) if entry.entry_id not in removals),
        key=sort_position,
    )


def _drop_oldest_context_unit(selected: list[EntryDraft]) -> None:
    """Drop one complete provider message unit while shrinking the window."""
    if not selected:
        return
    first = selected.pop(0)
    if first.kind == "tool_call":
        call_ids = set(_tool_call_ids(first))
        selected[:] = [
            entry
            for entry in selected
            if not (
                entry.kind == "tool_result"
                and _tool_result_call_id(entry) in call_ids
            )
        ]
        return
    if first.kind != "tool_result":
        return
    call_id = _tool_result_call_id(first)
    if not call_id:
        return
    for index, entry in enumerate(selected):
        if entry.kind != "tool_call" or call_id not in _tool_call_ids(entry):
            continue
        selected.pop(index)
        return


def _tool_result_call_id(entry: EntryDraft) -> str:
    return str(entry.payload.get("call_id") or "")


def _tool_call_ids(entry: EntryDraft) -> tuple[str, ...]:
    return tuple(
        identity
        for call in _tool_calls(entry)
        if mapping_fields(call) and isinstance(identity := call.get("id"), str) and identity
    )


def _tool_calls(entry: EntryDraft) -> Sequence[object]:
    value = entry.payload.get("tool_calls")
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        return ()
    calls: Sequence[object] = value
    return calls


def _missing_tool_result_text(entry: EntryDraft, call_id: str) -> str:
    """Describe an unfinished call without inventing an external outcome."""
    for call in _tool_calls(entry):
        if not mapping_fields(call) or str(call.get("id") or "") != call_id:
            continue
        function = call.get("function")
        function_name = function.get("name") if mapping_fields(function) else None
        name = str(call.get("name") or function_name or "")
        if name == "ask_user":
            return "No execution authorized: the user question was answered outside the provider turn."
        break
    return "Tool result unavailable: history ends before this call completed."


def _find_tool_result(
    candidates: Sequence[EntryDraft], call_position: int, call_id: str,
) -> EntryDraft | None:
    for candidate in candidates[call_position + 1:]:
        if candidate.kind != "tool_result":
            continue
        if str(candidate.payload.get("call_id") or "") == call_id:
            return candidate
    return None


def _find_tool_call(candidates: Sequence[EntryDraft], result_position: int, call_id: object) -> EntryDraft | None:
    for previous in reversed(candidates[:result_position]):
        if previous.kind != "tool_call":
            continue
        ids = _tool_call_ids(previous)
        matches = not ids if call_id is None else str(call_id) in ids
        if matches:
            return previous
    return None


def _apply_context_edits(entries: Sequence[EntryDraft]) -> tuple[EntryDraft, ...]:
    # Fold in path order: a later checkpoint may edit the view produced by an
    # earlier one. Each immutable source retains its position and an editable
    # replacement tree; editing the original anchor replaces that whole tree.
    originals: dict[str, EntryDraft] = {}
    views: dict[str, list[EntryDraft]] = {}
    roots: dict[str, str] = {}
    for entry in entries:
        if entry.kind != "context_edit":
            originals[entry.entry_id] = entry
            views[entry.entry_id] = [entry]
            roots[entry.entry_id] = entry.entry_id
        payload = entry.payload
        raw_edits = payload.get("edits", payload.get("context_edits"))
        if entry.kind != "context_edit" and raw_edits is None:
            continue
        if not isinstance(raw_edits, Sequence) or isinstance(raw_edits, (str, bytes)):
            raise ValueError("context edits must be a list")
        edits: Sequence[object] = raw_edits
        for raw_edit in edits:
            if not mapping_fields(raw_edit):
                raise ValueError("context edit must be an object")
            target = raw_edit.get("target")
            action = raw_edit.get("action")
            if not isinstance(target, str) or not target:
                raise ValueError("context edit target is required")
            if action not in {"omit", "replace"}:
                raise ValueError(f"unknown context edit action: {action!r}")
            root = roots.get(target)
            if root is None:
                continue
            current = views[root]
            if target in originals:
                source, start, end = originals[target], 0, len(current)
            else:
                start = next((i for i, item in enumerate(current) if item.entry_id == target), -1)
                if start < 0:
                    continue
                source, end = current[start], start + 1
            replacement = _context_replacement(source, raw_edit) if action == "replace" else []
            current[start:end] = replacement
            roots.update((item.entry_id, root) for item in replacement)
    return tuple(item for view in views.values() for item in view)


def _context_replacement(source: EntryDraft, edit: Mapping[str, object]) -> list[EntryDraft]:
    messages = edit.get("messages")
    if not isinstance(messages, Sequence) or isinstance(messages, (str, bytes)):
        raise ValueError("replacement context messages must be a list")
    result: list[EntryDraft] = []
    values: Sequence[object] = messages
    for index, message in enumerate(values):
        if not mapping_fields(message):
            raise ValueError("replacement context message must be an object")
        result.append(EntryDraft(
            f"{source.entry_id}:replacement:{index}", source.conversation_id, source.parent_id,
            _kind_for_message(message), _payload_for_message(message),
        ))
    return result


def _kind_for_message(message: Mapping[str, object]) -> str:
    role = message.get("role")
    if role == "user":
        return "user_message"
    if role == "assistant":
        return "tool_call" if message.get("tool_calls") else "assistant_message"
    if role == "tool":
        return "tool_result"
    return "system_message"


def _payload_for_message(message: Mapping[str, object]) -> dict[str, object]:
    role = message.get("role")
    if role in {"user", "assistant"}:
        payload: dict[str, object] = {"text": str(message.get("content") or "")}
        if message.get("status") is not None:
            payload["status"] = message["status"]
        if message.get("tool_calls") is not None:
            payload["tool_calls"] = message["tool_calls"]
        return payload
    if role == "tool":
        return {
            "call_id": message.get("tool_call_id"),
            "content": message.get("content") or "",
            "is_error": message.get("is_error") is True,
        }
    return {"content": message.get("content") or ""}


def _source_entry_id(entry_id: str) -> str:
    marker = ":replacement:"
    return entry_id.split(marker, 1)[0]


__all__ = ["ContextSnapshot", "build_context"]
