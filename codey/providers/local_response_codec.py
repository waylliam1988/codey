"""Response codec for local providers that expose text tool-call frames.

Some local model templates return provider-specific markup when native tools
are not enabled.  This module turns one complete supported frame into the
standard provider turn consumed by the kernel.  The kernel never parses a
model template directly.
"""

from __future__ import annotations

import hashlib
import json
import re
from typing import cast

from codey.providers.base import AssistantTurn, ProviderToolCall, TurnFinish
from codey.providers.local_literal import parse_mapping

_FRAME_PREFIXES = ("<|tool_call>call:tool:", "<|tool_call>call:")
_FRAME_SUFFIX = "<tool_call|>"
_TOOL_NAME_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*\Z")
_GEMMA_REASONING_RE = re.compile(
    r"^\s*\[TOOLCALL\s+REASONING\]\s*:\s*(\{.*\})\s*$",
    re.DOTALL,
)
_GEMMA_METADATA_LIMIT = 64 * 1024


def parse_local_tool_markup(text: str) -> ProviderToolCall | None:
    """Parse one complete supported local tool frame, or return ``None``.

    The entire response must be exactly one frame. Partial frames, prose
    around a frame, and non-literal arguments are not parsed as calls.
    Argument shape and policy are validated later by ``kernel_protocol``.
    """
    if type(text) is not str or not text.endswith(_FRAME_SUFFIX):
        return None
    prefix = next((item for item in _FRAME_PREFIXES if text.startswith(item)), None)
    if prefix is None:
        return None
    body = text[len(prefix):-len(_FRAME_SUFFIX)]
    brace = body.find("{")
    if brace <= 0 or not body.endswith("}"):
        return None
    name = body[:brace]
    if _TOOL_NAME_RE.fullmatch(name) is None:
        return None
    arguments = parse_mapping(body[brace:])
    if arguments is None:
        return None
    if "args" in arguments:
        if len(arguments) != 1 or type(arguments["args"]) is not dict:
            return None
        arguments = cast(dict[str, object], arguments["args"])
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


def normalize_local_reply(reply: str | AssistantTurn) -> str | AssistantTurn:
    """Convert a complete local text frame into the canonical provider turn."""
    if isinstance(reply, AssistantTurn):
        return reply
    reasoning = parse_gemma_tool_reasoning(reply)
    if reasoning is not None:
        return reasoning
    call = parse_local_tool_markup(reply)
    if call is None:
        if reply.lstrip().startswith("<|tool_call>"):
            frame = reply.strip()
            name = re.match(r"<\|tool_call>call:(?:tool:)?([A-Za-z_][A-Za-z0-9_]*)\{", frame)
            detail = ("Local tool frame is missing the closing <tool_call|> marker. "
                      "Resend a complete call as bare JSON, without tool-call markers."
                      if not frame.endswith(_FRAME_SUFFIX) else
                      "Local tool frame has invalid argument syntax. Use quoted strings for text, including enum choices; "
                      "do not emit bare words such as state:reported.")
            return AssistantTurn(text="", finish=TurnFinish.PROTOCOL_ERROR,
                                 raw={"codec": "local-text", "malformed_tool_frame": True,
                                      "protocol_error": detail, "protocol_tool_name": name.group(1) if name else ""})
        return reply
    return AssistantTurn(text="", tool_calls=(call,), raw={"codec": "local-text"})


__all__ = [
    "normalize_local_reply",
    "parse_gemma_tool_reasoning",
    "parse_local_tool_markup",
]
