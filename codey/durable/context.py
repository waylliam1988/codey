"""Pure context construction from durable conversation entries."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, replace

from codey.durable.model import EntryDraft, thaw_json
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
    payload = raw_payload if isinstance(raw_payload, dict) else {}
    if entry.kind == "user_message":
        return {"role": "user", "content": str(payload.get("text") or "")}
    if entry.kind == "assistant_message":
        return {"role": "assistant", "content": str(payload.get("text") or "")}
    if entry.kind == "tool_call":
        return {"role": "assistant", "tool_calls": payload.get("tool_calls", [payload])}
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
    payload = thaw_json(entry.payload)
    if not isinstance(payload, Mapping):
        return False
    status = payload.get("status")
    return isinstance(status, str) and status.casefold() in {
        "aborted",
        "error",
        "interrupted",
    }


def _is_display_only(entry: EntryDraft) -> bool:
    payload = thaw_json(entry.payload)
    return isinstance(payload, Mapping) and payload.get("display_only") is True


def _summary_text(value: object) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, Mapping):
        return json.dumps(dict(value), ensure_ascii=False, sort_keys=True)
    return str(value or "")


def _estimate_tokens(value: object) -> int:
    if value == () or value == []:
        return 0
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return estimate_tokens(encoded)


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
        position = index[entry.entry_id]
        payload = thaw_json(entry.payload)
        result_call_id: object = payload.get("call_id") if isinstance(payload, Mapping) else None
        match = None
        for previous in reversed(candidates[:position]):
            if previous.kind != "tool_call":
                continue
            previous_payload = thaw_json(previous.payload)
            calls = previous_payload.get("tool_calls") if isinstance(previous_payload, dict) else None
            ids = {
                str(call.get("id"))
                for call in calls or ()
                if isinstance(call, Mapping) and call.get("id") is not None
            }
            matches = not ids if result_call_id is None else str(result_call_id) in ids
            if matches:
                match = previous
                break
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
    return sorted(
        (entry for entry in (*selected, *additions) if entry.entry_id not in removals),
        key=lambda entry: (
            index[entry.entry_id] if entry.entry_id in index else index[entry.parent_id or ""],
            entry.entry_id not in index,
        ),
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
        if entry.kind != "tool_call" or str(call_id) not in _tool_call_ids(entry):
            continue
        selected.pop(index)
        return


def _tool_result_call_id(entry: EntryDraft) -> str:
    payload = thaw_json(entry.payload)
    if not isinstance(payload, Mapping):
        return ""
    return str(payload.get("call_id") or "")


def _tool_call_ids(entry: EntryDraft) -> tuple[str, ...]:
    payload = thaw_json(entry.payload)
    calls = payload.get("tool_calls") if isinstance(payload, Mapping) else None
    if not isinstance(calls, Sequence) or isinstance(calls, (str, bytes)):
        return ()
    return tuple(
        str(call.get("id"))
        for call in calls
        if isinstance(call, Mapping) and isinstance(call.get("id"), str) and call.get("id")
    )


def _missing_tool_result_text(entry: EntryDraft, call_id: str) -> str:
    """Describe an unfinished call without inventing an external outcome."""
    payload = thaw_json(entry.payload)
    calls = payload.get("tool_calls") if isinstance(payload, Mapping) else None
    for call in calls or ():
        if not isinstance(call, Mapping) or str(call.get("id") or "") != call_id:
            continue
        function = call.get("function")
        function_name = function.get("name") if isinstance(function, Mapping) else None
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
        payload = thaw_json(candidate.payload)
        if isinstance(payload, Mapping) and str(payload.get("call_id") or "") == call_id:
            return candidate
    return None


def _apply_context_edits(entries: Sequence[EntryDraft]) -> tuple[EntryDraft, ...]:
    edits: dict[str, Mapping[str, object]] = {}
    for entry in entries:
        payload = thaw_json(entry.payload)
        if not isinstance(payload, Mapping):
            continue
        raw_edits = payload.get("edits", payload.get("context_edits"))
        if entry.kind != "context_edit" and raw_edits is None:
            continue
        if not isinstance(raw_edits, Sequence) or isinstance(raw_edits, (str, bytes)):
            raise ValueError("context edits must be a list")
        for raw_edit in raw_edits:
            if not isinstance(raw_edit, Mapping):
                raise ValueError("context edit must be an object")
            target = raw_edit.get("target")
            action = raw_edit.get("action")
            if not isinstance(target, str) or not target:
                raise ValueError("context edit target is required")
            if action not in {"omit", "replace"}:
                raise ValueError(f"unknown context edit action: {action!r}")
            edits[target] = raw_edit

    result: list[EntryDraft] = []
    for entry in entries:
        if entry.kind == "context_edit":
            continue
        edit = edits.get(entry.entry_id)
        if edit is None:
            result.append(entry)
            continue
        if edit.get("action") == "omit":
            continue
        messages = edit.get("messages")
        if not isinstance(messages, Sequence) or isinstance(messages, (str, bytes)):
            raise ValueError("replacement context messages must be a list")
        for index, message in enumerate(messages):
            if not isinstance(message, Mapping):
                raise ValueError("replacement context message must be an object")
            result.append(
                EntryDraft(
                    f"{entry.entry_id}:replacement:{index}",
                    entry.conversation_id,
                    entry.parent_id,
                    _kind_for_message(message),
                    _payload_for_message(message),
                )
            )
    return tuple(result)


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
