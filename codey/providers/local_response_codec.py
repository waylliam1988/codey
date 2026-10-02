"""Response codec for local providers that expose text tool-call frames.

Some local model templates return provider-specific markup when native tools
are not enabled.  This module turns one complete supported frame into the
standard provider turn consumed by the kernel.  The kernel never parses a
model template directly.
"""

from __future__ import annotations

import ast
import hashlib
import json
import re
from typing import Any

from codey.providers.base import AssistantTurn, ProviderToolCall

_FRAME_PREFIX = "<|tool_call>call:tool:"
_FRAME_SUFFIX = "<tool_call|>"
_TOOL_NAME_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*\Z")
_GEMMA_REASONING_RE = re.compile(
    r"^\s*\[TOOLCALL\s+REASONING\]\s*:\s*(\{.*\})\s*$",
    re.DOTALL,
)
_GEMMA_METADATA_LIMIT = 64 * 1024


def _quote_bare_keys(source: str) -> str:
    """Quote identifier keys outside string literals for literal_eval."""
    out: list[str] = []
    index = 0
    quote = ""
    escaped = False
    while index < len(source):
        char = source[index]
        if quote:
            out.append(char)
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == quote:
                quote = ""
            index += 1
            continue
        if char in {"'", '"'}:
            quote = char
            out.append(char)
            index += 1
            continue
        if char.isalpha() or char == "_":
            end = index + 1
            while end < len(source) and (source[end].isalnum() or source[end] == "_"):
                end += 1
            cursor = end
            while cursor < len(source) and source[cursor].isspace():
                cursor += 1
            if cursor < len(source) and source[cursor] == ":":
                out.append(repr(source[index:end]))
                index = end
                continue
        out.append(char)
        index += 1
    if quote:
        raise ValueError("unterminated string literal")
    return "".join(out)


def _literal_mapping(source: str) -> dict[str, Any] | None:
    try:
        value = ast.literal_eval(_quote_bare_keys(source))
    except (SyntaxError, ValueError, TypeError, MemoryError, RecursionError):
        return None
    if type(value) is not dict or any(type(key) is not str for key in value):
        return None
    return value


def parse_local_tool_markup(text: str) -> ProviderToolCall | None:
    """Parse one complete supported local tool frame, or return ``None``.

    The entire response must be exactly one frame.  Partial frames, prose
    around a frame, and non-literal arguments remain ordinary assistant text.
    Argument shape and policy are validated later by ``kernel_protocol``.
    """
    if type(text) is not str or not text.startswith(_FRAME_PREFIX) or not text.endswith(_FRAME_SUFFIX):
        return None
    body = text[len(_FRAME_PREFIX):-len(_FRAME_SUFFIX)]
    brace = body.find("{")
    if brace <= 0 or not body.endswith("}"):
        return None
    name = body[:brace]
    if _TOOL_NAME_RE.fullmatch(name) is None:
        return None
    arguments = _literal_mapping(body[brace:])
    if arguments is None:
        return None
    if "args" in arguments:
        if len(arguments) != 1 or type(arguments["args"]) is not dict:
            return None
        arguments = arguments["args"]
    if any(type(key) is not str for key in arguments):
        return None
    call_id = "local-" + hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]
    return ProviderToolCall(id=call_id, name=name, arguments=dict(arguments))


def parse_gemma_tool_reasoning(text: str) -> AssistantTurn | None:
    """Capture Gemma's diagnostic decision frame without treating it as a call.

    KoboldCpp/Gemma may emit ``final_decision`` and ``tool_name`` in ordinary
    text.  Those fields are model metadata, not the tool protocol.  Returning
    an ``AssistantTurn`` with no calls makes the kernel fail closed while
    preserving the bounded raw evidence for provider diagnostics.
    """
    if type(text) is not str or len(text) > _GEMMA_METADATA_LIMIT:
        return None
    match = _GEMMA_REASONING_RE.fullmatch(text)
    if match is None:
        return None
    try:
        payload = json.loads(match.group(1))
    except (json.JSONDecodeError, TypeError, ValueError):
        return None
    if not isinstance(payload, dict):
        return None
    metadata = {str(key): value for key, value in payload.items() if str(key) in {
        "reasoning", "final_decision", "tool_name"
    }}
    return AssistantTurn(
        text="",
        tool_calls=(),
        raw={"codec": "gemma-tool-reasoning", "provider_metadata": metadata},
    )


def normalize_local_reply(reply: str) -> str | AssistantTurn:
    """Convert a complete local text frame into the canonical provider turn."""
    reasoning = parse_gemma_tool_reasoning(reply)
    if reasoning is not None:
        return reasoning
    call = parse_local_tool_markup(reply)
    if call is None:
        return reply
    return AssistantTurn(text="", tool_calls=(call,), raw={"codec": "local-text"})


__all__ = [
    "normalize_local_reply",
    "parse_gemma_tool_reasoning",
    "parse_local_tool_markup",
]
