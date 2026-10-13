"""SSE framing and Chat Completions tool-call assembly."""
from __future__ import annotations

import time
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from codey.providers.api_transport import AttemptControl


def _merge_call(calls: dict[int, dict[str, Any]], call: object) -> None:
    """Resolve omitted indices only from an unambiguous call identity."""
    if not isinstance(call, dict):
        raise ValueError("tool delta is not an object")
    identity = call.get("id")
    if identity is not None and (not isinstance(identity, str) or not identity):
        raise ValueError("tool delta has an invalid id")
    index = call.get("index")
    if index is not None:
        if type(index) is not int or index < 0:
            raise ValueError("tool delta has an invalid index")
    elif identity:
        matches = [key for key, value in calls.items() if value["id"] == identity]
        index = matches[0] if matches else max(calls, default=-1) + 1
    elif len(calls) == 1:
        index = next(iter(calls))
    else:
        raise ValueError("tool delta without index or id is ambiguous")
    slot = calls.setdefault(index, {"id": "", "type": "function",
                                    "function": {"name": "", "arguments": ""}})
    if identity:
        if slot["id"] and slot["id"] != identity:
            raise ValueError("tool delta changed its call id")
        if any(key != index and value["id"] == identity for key, value in calls.items()):
            raise ValueError("tool delta reused an id at another index")
        slot["id"] = identity
    function = call.get("function") or {}
    if not isinstance(function, dict):
        raise ValueError("tool delta has an invalid function")
    for key in ("name", "arguments"):
        fragment = function.get(key) or ""
        if not isinstance(fragment, str):
            raise ValueError("tool delta has a non-text fragment")
        slot["function"][key] += fragment


def read_sse(response: Any, *, deadline: float, cancelled: Callable[[], bool],
             on_event: Callable[[dict[str, Any]], None] = lambda event: None,
             control: AttemptControl | None = None,
             idle_timeout: float | None = None) -> dict[str, Any]:
    from codey.providers.api_transport import (
        MAX_RESPONSE_BYTES,
        BoundedBodyReader,
        GenerationUnknownError,
        _json_object,
    )

    data: list[bytes] = []
    total = 0
    chat: dict[str, Any] = {"content": "", "tool_calls": []}
    calls: dict[int, dict[str, Any]] = {}
    chat_finish = ""
    chat_done = False
    reader = BoundedBodyReader(response, control=control, deadline=deadline, idle_timeout=idle_timeout)
    while True:
        if cancelled():
            raise GenerationUnknownError("generation cancelled; late response discarded")
        if time.monotonic() >= deadline:
            raise GenerationUnknownError("generation deadline exceeded; not resent")
        line = reader.readline(MAX_RESPONSE_BYTES + 1)
        total += len(line)
        if total > MAX_RESPONSE_BYTES:
            raise GenerationUnknownError("generation stream exceeded its byte limit")
        if not line:
            break
        if line.strip():
            if line.startswith(b"data:"):
                data.append(line[5:].strip())
            continue
        if not data:
            continue
        frame = b"\n".join(data)
        data = []
        if frame == b"[DONE]":
            chat_done = True
            break
        event = _json_object(frame)
        on_event(event)
        kind = event.get("type")
        if kind in {"response.completed", "response.incomplete", "response.failed"}:
            result = event.get("response")
            if not isinstance(result, dict):
                raise GenerationUnknownError("Responses stream ended without a response object")
            return result
        if kind == "error" or "error" in event:
            raise RuntimeError(f"generation stream error: {str(event.get('error', event))[:400]}")
        for choice in event.get("choices", []):
            delta = choice.get("delta") or {}
            chat["content"] += delta.get("content") or ""
            for name in ["reasoning_content", "reasoning"]:
                if delta.get(name):
                    chat[name] = chat.get(name, "") + delta[name]
            for call in delta.get("tool_calls") or []:
                try:
                    _merge_call(calls, call)
                except ValueError as exc:
                    raise GenerationUnknownError(f"malformed streamed tool call: {exc}; not resent") from exc
            chat_finish = choice.get("finish_reason") or chat_finish
    if chat_finish and chat_done:
        chat["tool_calls"] = [calls[key] for key in sorted(calls)]
        return {"choices": [{"message": chat, "finish_reason": chat_finish}]}
    raise GenerationUnknownError("generation stream ended without a valid terminal event; not resent")
