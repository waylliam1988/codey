"""SSE framing and Chat Completions tool-call assembly."""
from __future__ import annotations

import time
from collections.abc import Callable
from typing import TYPE_CHECKING, Protocol

from codey.types.json_value import (
    JsonObject,
    JsonValue,
    decode_json_object,
    parse_json_object,
    require_json_integer,
    require_json_string,
)

if TYPE_CHECKING:
    from codey.providers.api_transport import AttemptControl


def _merge_call(calls: dict[int, JsonObject], call: object) -> None:
    """Resolve omitted indices only from an unambiguous call identity."""
    call = decode_json_object(call)
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
    target = decode_json_object(slot["function"])
    for key in ("name", "arguments"):
        fragment = function.get(key) or ""
        if not isinstance(fragment, str):
            raise ValueError("tool delta has a non-text fragment")
        target[key] = require_json_string(target[key], key) + fragment
    slot["function"] = target


class ChatStreamAccumulator(Protocol):
    def add_delta(self, delta: JsonObject) -> None: ...
    def finish_message(self) -> JsonObject: ...


class DefaultChatAccumulator:
    def __init__(self) -> None:
        self.chat: JsonObject = {"content": "", "tool_calls": []}
        self.calls: dict[int, JsonObject] = {}

    def add_delta(self, delta: JsonObject) -> None:
        self.chat["content"] = require_json_string(self.chat["content"], "content") + require_json_string(delta.get("content") or "", "delta content")
        for name in ("reasoning_content", "reasoning", "thinking"):
            fragment = delta.get(name)
            if isinstance(fragment, str):
                self.chat[name] = require_json_string(self.chat.get(name, ""), name) + fragment
        calls = delta.get("tool_calls")
        if calls is not None and not isinstance(calls, list):
            raise ValueError("tool call deltas must be an array")
        for call in calls or ():
            try:
                _merge_call(self.calls, call)
            except ValueError as exc:
                from codey.providers.api_transport import GenerationUnknownError

                raise GenerationUnknownError(f"malformed streamed tool call: {exc}; not resent") from exc

    def finish_message(self) -> JsonObject:
        self.chat["tool_calls"] = list[JsonValue](self.calls[key] for key in sorted(self.calls))
        return self.chat


class MessagesStream:
    def __init__(self, event: JsonObject) -> None:
        self.message = decode_json_object(event.get("message"))
        self.blocks: list[JsonObject] = []
        self.current: JsonObject | None = None
        self.arguments: list[str] = []
        self.start = self.message.copy()

    def is_repeated_start(self, event: JsonObject) -> bool:
        # Some compatible servers emit the same start twice before a tool block.
        return (not self.blocks and self.current is None and self.message == self.start
                and event.get("message") == self.start)

    def update(self, event: JsonObject) -> JsonObject | None:
        kind = event.get("type")
        if kind == "content_block_start":
            if self.current is not None or require_json_integer(event.get("index"), "content block index") != len(self.blocks):
                raise ValueError("invalid Messages content block order")
            self.current = decode_json_object(event.get("content_block"))
        elif kind in {"content_block_delta", "content_block_stop"}:
            if self.current is None or require_json_integer(event.get("index"), "content block index") != len(self.blocks):
                raise ValueError("Messages content block has no matching start")
            if kind == "content_block_delta":
                self._append_delta(decode_json_object(event.get("delta")), self.current)
            else:
                if self.arguments:
                    self.current["input"] = parse_json_object("".join(self.arguments))
                self.blocks.append(self.current)
                self.current = None
                self.arguments.clear()
        elif kind == "message_delta":
            self.message.update(decode_json_object(event.get("delta")))
            self.message["usage"] = {**decode_json_object(self.message.get("usage", {})),
                                     **decode_json_object(event.get("usage", {}))}
        elif kind == "message_stop":
            if self.current is not None or not self.message.get("stop_reason"):
                raise ValueError("Messages stream ended with unfinished content")
            self.message["content"] = list[JsonValue](self.blocks)
            return self.message
        return None

    def _append_delta(self, delta: JsonObject, block: JsonObject) -> None:
        kind = require_json_string(delta.get("type"), "content delta type")
        if kind == "input_json_delta":
            self.arguments.append(require_json_string(delta.get("partial_json"), "tool input fragment"))
            return
        key = {"text_delta": "text", "thinking_delta": "thinking", "signature_delta": "signature"}.get(kind)
        if key is not None:
            block[key] = require_json_string(block.get(key, ""), key) + require_json_string(delta.get(key), key)


def _responses_terminal(event: JsonObject, error_detail_filter: Callable[[int, str], str] | None) -> JsonObject | None:
    from codey.providers.api_transport import GenerationUnknownError

    kind = event.get("type")
    if kind in ("response.completed", "response.incomplete", "response.failed"):
        result = event.get("response")
        if not isinstance(result, dict):
            raise GenerationUnknownError("Responses stream ended without a response object")
        return decode_json_object(result)
    if kind == "error" or "error" in event:
        detail = str(event.get("error", event))
        if error_detail_filter is not None:
            detail = error_detail_filter(502, detail)
        raise RuntimeError(f"generation stream error: {detail[:400]}")
    return None


def _chat_finish(event: JsonObject, accumulator: ChatStreamAccumulator) -> str:
    from codey.providers.api_transport import GenerationUnknownError

    choices = event.get("choices", [])
    if not isinstance(choices, list):
        raise GenerationUnknownError("invalid streamed choices; not resent")
    finish = ""
    for raw in choices:
        choice = decode_json_object(raw)
        if choice.get("index", 0) != 0:
            raise GenerationUnknownError("multiple streamed choices are unsupported; not resent")
        try:
            accumulator.add_delta(decode_json_object(choice.get("delta") or {}))
        except ValueError as exc:
            raise GenerationUnknownError(f"malformed streamed message: {exc}; not resent") from exc
        if isinstance(reason := choice.get("finish_reason"), str) and reason:
            finish = reason
    return finish


def read_sse(response: object, *, deadline: float, cancelled: Callable[[], bool],
             on_event: Callable[[JsonObject], None] = lambda event: None,
             control: AttemptControl | None = None,
             idle_timeout: float | None = None,
             chat_accumulator_factory: Callable[[], ChatStreamAccumulator] = DefaultChatAccumulator,
             error_detail_filter: Callable[[int, str], str] | None = None) -> JsonObject:
    from codey.providers.api_transport import (
        MAX_RESPONSE_BYTES,
        BoundedBodyReader,
        GenerationUnknownError,
    )

    data: list[bytes] = []
    total = 0
    accumulator = chat_accumulator_factory()
    chat_finish = ""
    chat_done = False
    messages: MessagesStream | None = None
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
        event = parse_json_object(frame.decode("utf-8"))
        on_event(event)
        terminal = _responses_terminal(event, error_detail_filter)
        if terminal is not None:
            return terminal
        if event.get("type") == "message_start":
            if messages is None:
                messages = MessagesStream(event)
            elif not messages.is_repeated_start(event):
                raise GenerationUnknownError("Messages stream has duplicate starts; not resent")
            continue
        if messages is not None:
            try:
                completed = messages.update(event)
            except ValueError as exc:
                raise GenerationUnknownError(f"malformed Messages stream: {exc}; not resent") from exc
            if completed is not None:
                on_event(completed)
                return completed
            continue
        chat_finish = _chat_finish(event, accumulator) or chat_finish
    if chat_finish and chat_done:
        if cancelled():
            raise GenerationUnknownError("generation cancelled; late response discarded")
        try:
            chat = accumulator.finish_message()
        except ValueError as exc:
            raise GenerationUnknownError(f"malformed streamed message: {exc}; not resent") from exc
        return {"choices": [{"message": chat, "finish_reason": chat_finish}]}
    raise GenerationUnknownError("generation stream ended without a valid terminal event; not resent")
