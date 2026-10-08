"""Chat Completions wire encoding and atomic history preparation."""
from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any, cast

from codey.providers.api_codec import GenerationSettings
from codey.providers.base import AssistantTurn, ProviderToolCall, ProviderToolDefinition, ProviderToolResult, TurnFinish
from codey.toolchain.tool_spec import thaw_schema_value

endpoint = "chat/completions"


def decode_exchange(body: dict[str, Any], *, text_only: bool,
                    text_decoder: Callable[[str], str | AssistantTurn] | None,
                    ) -> tuple[AssistantTurn, list[dict[str, Any]] | None]:
    if text_only:
        text = _extract_reply(body)
        choice = body["choices"][0]
        message = choice.get("message") or {"content": text}
        _parse_tool_calls(message)
        if message.get("tool_calls"):
            raise RuntimeError("unexpected native tool calls in a text-only exchange")
        turn = AssistantTurn(text=text, reasoning=_reasoning_text(message))
        return turn, [_store_assistant_message(message)]
    turn, stored = decode_turn(decode_message(body), text_decoder=text_decoder)
    return turn, [stored] if stored is not None else None


def encode_tools(tools: list[ProviderToolDefinition] | None) -> list[dict[str, Any]]:
    return [{"type": "function", "function": {"name": t.name, "description": t.description,
             "parameters": thaw_schema_value(t.parameters)}} for t in tools or []]


def encode_results(results: list[ProviderToolResult]) -> list[dict[str, Any]]:
    return [{"role": "tool", "tool_call_id": item.call_id, "content": item.content} for item in results]


def prepare(history: list[dict[str, Any]], pending: list[dict[str, Any]], *, system: str,
            tools: list[ProviderToolDefinition] | None, budget: tuple[int, int, int]) -> list[dict[str, Any]]:
    from codey.agents import context_compaction as compaction
    from codey.providers import error_classification as errors

    candidate: list[dict[str, object]] = [dict(message) for message in history]
    if not candidate and system:
        candidate.append({"role": "system", "content": system})
    candidate.extend(dict(item) for item in pending)
    window, reserve, keep = budget
    groups = compaction.group_messages_for_compaction(candidate)
    if any(not compaction.is_tool_group_complete(candidate, group) for group in groups):
        raise errors.RequestPrepError("local tool history has incomplete or invalid ID pairing")
    declarations = encode_tools(tools)
    try:
        compaction.compact_openai_messages_in_place(candidate, tools=declarations,
                                                   context_window_tokens=window, reserve_tokens=reserve, keep_recent_tokens=keep)
        estimated = compaction.estimate_messages_tokens(candidate) + compaction.estimate_tools_tokens(declarations) + reserve
    except Exception as exc:
        raise errors.RequestPrepError(f"local context compaction failed: {exc}") from exc
    if estimated > window:
        raise errors.ContextOverflowError(f"local model context overflow before send: estimated {estimated} tokens exceeds window {window} (reserve {reserve} for reply)")
    return candidate


def build_payload(messages: list[dict[str, Any]], tools: list[ProviderToolDefinition] | None,
                  settings: GenerationSettings) -> dict[str, Any]:
    body: dict[str, Any] = {"model": settings.model, "messages": messages, "temperature": settings.temperature, "stream": settings.stream}
    if settings.thinking is not None:
        body["chat_template_kwargs"] = {"enable_thinking": settings.thinking}
    if settings.effort is not None:
        body["reasoning_effort"] = "none" if settings.effort == "off" else settings.effort
    if settings.output_tokens is not None:
        body["max_tokens"] = settings.output_tokens
    if tools:
        body.update(tools=encode_tools(tools), tool_choice=settings.choice, parallel_tool_calls=False)
    elif tools is not None and messages and messages[-1].get("role") == "tool":
        body["max_tokens"] = 1
    return body

def _parse_tool_calls(message: dict[str, Any]) -> tuple[list[dict[str, object]], int]:
    """Parse raw tool_calls, returning the usable calls plus a drop count.

    A call without an id, without a name, or without a JSON-object
    ``arguments`` payload can never be answered legally, so callers must
    treat any drop as a malformed turn (fail closed) rather than storing
    the raw block. Illegal arguments are never coerced to ``{}``: a
    ``done`` with ``"{bad"`` must not become a valid completion.
    A present-but-non-list ``tool_calls`` is a protocol error, never an
    empty turn: it would silently downgrade a tool turn to plain text.
    """
    if "tool_calls" not in message:
        return [], 0
    raw_calls = message.get("tool_calls")
    if raw_calls is None:
        return [], 0
    if not isinstance(raw_calls, list):
        raise RuntimeError("local model returned malformed tool_calls")
    parsed: list[dict[str, object]] = []
    dropped = 0
    seen_ids: set[str] = set()
    for item in raw_calls:
        if not isinstance(item, dict):
            dropped += 1
            continue
        call_id = item.get("id")
        function = item.get("function")
        if not isinstance(function, dict):
            dropped += 1
            continue
        name = str(function.get("name") or "")
        if type(call_id) is not str or not call_id.strip() or call_id in seen_ids or not name:
            dropped += 1
            continue
        seen_ids.add(call_id)
        raw_args = function.get("arguments")
        if isinstance(raw_args, dict):
            arguments = dict(raw_args)
        elif isinstance(raw_args, str):
            if not raw_args.strip():
                dropped += 1
                continue
            try:
                decoded = json.loads(raw_args)
            except json.JSONDecodeError:
                dropped += 1
                continue
            if not isinstance(decoded, dict):
                dropped += 1
                continue
            arguments = dict(decoded)
        else:
            dropped += 1
            continue
        parsed.append({"id": call_id, "name": name, "arguments": arguments})
    return parsed, dropped


def _store_assistant_message(message: dict[str, Any]) -> dict[str, Any]:
    stored: dict[str, object] = {"role": "assistant", "content": str(message.get("content") or "")}
    reasoning = _reasoning_text(message)
    if reasoning:
        stored["reasoning_content"] = reasoning
    raw_calls = message.get("tool_calls")
    if isinstance(raw_calls, list) and raw_calls:
        stored["tool_calls"] = raw_calls
    return stored


def _reasoning_text(message: object) -> str:
    if not isinstance(message, dict):
        return ""
    for key in ("reasoning_content", "reasoning", "thinking"):
        value = message.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def decode_turn(message: dict[str, Any], *,
                text_decoder: Callable[[str], str | AssistantTurn] | None = None,
                ) -> tuple[AssistantTurn, dict[str, Any] | None]:
    """Normalize text frames and native calls before runtime state commits."""
    parsed, dropped = _parse_tool_calls(message)
    text = str(message.get("content") or "")
    metadata_turn = text_decoder(text) if text_decoder is not None else text
    provider_metadata = {}
    if isinstance(metadata_turn, AssistantTurn):
        provider_metadata = dict(metadata_turn.raw)
        text = metadata_turn.text
        if not parsed and not dropped and not message.get("_continuable_length") and metadata_turn.tool_calls:
            parsed = [{"id": call.id, "name": call.name, "arguments": dict(call.arguments)} for call in metadata_turn.tool_calls]
            message = {**message, "content": text, "tool_calls": [
                {"id": call["id"], "type": "function", "function": {
                    "name": call["name"], "arguments": json.dumps(call["arguments"], ensure_ascii=False),
                }} for call in parsed
            ]}
    if dropped:
        # Content cannot rescue a malformed native batch as a JSON call.
        return AssistantTurn(text=f"ERROR: local model returned {dropped} malformed tool call(s)", raw={
            "finish_reason": str(message.get("_finish_reason") or ""), "malformed_dropped": dropped, **provider_metadata,
        }), None
    turn = AssistantTurn(text=text, reasoning=_reasoning_text(message), tool_calls=tuple(
        ProviderToolCall(id=str(call["id"]), name=str(call["name"]),
            arguments=cast(dict[str, Any], call.get("arguments")) if isinstance(call.get("arguments"), dict) else {}) for call in parsed),
        raw={"finish_reason": str(message.get("_finish_reason") or ""), **provider_metadata},
        finish=TurnFinish.OUTPUT_LIMIT if message.get("_continuable_length") else TurnFinish.COMPLETE)
    return turn, _store_assistant_message(message)


def _extract_reply(body: dict[str, Any]) -> str:
    try:
        choice = body["choices"][0]
    except (KeyError, IndexError, TypeError) as exc:
        raise RuntimeError("local model returned no choices") from exc
    if not isinstance(choice, dict):
        raise RuntimeError("local model returned a malformed choice")
    from codey.providers import error_classification as errors

    kind = errors.classify_openai_choice(choice)
    if kind == errors.ProviderErrorKind.CONTEXT_OVERFLOW:
        raise errors.ContextOverflowError("local model context overflow (finish_reason=length)")
    if kind == errors.ProviderErrorKind.OUTPUT_LENGTH:
        raise errors.OutputLengthError()
    if kind == errors.ProviderErrorKind.FATAL:
        raise RuntimeError(
            "local model content filtered "
            f"(finish_reason={choice.get('finish_reason')})"
        )
    message = choice.get("message")
    if isinstance(message, dict):
        return str(message.get("content") or "")
    if isinstance(choice.get("text"), str):
        return str(choice.get("text") or "")
    raise RuntimeError("local model returned a choice without message content")



def decode_message(body: dict[str, Any]) -> dict[str, Any]:
    from codey.providers import error_classification as errors

    try:
        choice = body["choices"][0]
    except (KeyError, IndexError, TypeError) as exc:
        raise RuntimeError("local model returned no choices") from exc
    if not isinstance(choice, dict):
        raise RuntimeError("local model returned a malformed choice")
    kind = errors.classify_openai_choice(choice)
    if kind == errors.ProviderErrorKind.CONTEXT_OVERFLOW:
        raise errors.ContextOverflowError("local model context overflow (finish_reason=length)")
    if kind == errors.ProviderErrorKind.FATAL:
        raise RuntimeError(
            "local model content filtered "
            f"(finish_reason={choice.get('finish_reason')})"
        )
    message = choice.get("message")
    if not isinstance(message, dict):
        raise RuntimeError("local model returned a choice without message content")
    raw_calls = message.get("tool_calls")
    if kind == errors.ProviderErrorKind.OUTPUT_LENGTH and raw_calls:
        raise errors.OutputLengthError(
            "model output truncated while emitting tool calls; refusing partial tool execution"
        )
    out: dict[str, object] = {
        "content": message.get("content") or "",
        "_finish_reason": str(choice.get("finish_reason") or ""),
    }
    reasoning = _reasoning_text(message)
    if reasoning:
        out["reasoning_content"] = reasoning
    if kind == errors.ProviderErrorKind.OUTPUT_LENGTH:
        # Preserve a text-only length stop as a continuable assistant turn.
        # Tool calls are parsed below and malformed/truncated arguments
        # remain fail-closed; the kernel may spend one normal turn asking
        # the local provider to continue or call done.
        out["_continuable_length"] = True
    if "tool_calls" in message and not isinstance(message.get("tool_calls"), list):
        raise RuntimeError("local model returned malformed tool_calls")
    if isinstance(raw_calls, list):
        out["tool_calls"] = raw_calls
    return out
