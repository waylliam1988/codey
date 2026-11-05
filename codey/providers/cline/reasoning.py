"""Compile declared controls into the Cline gateway's reasoning structure."""
from __future__ import annotations

from typing import Any


def configure(payload: dict[str, Any]) -> None:
    effort = payload.pop("reasoning_effort", None)
    template = payload.get("chat_template_kwargs")
    thinking = template.pop("enable_thinking", None) if isinstance(template, dict) else None
    if template == {}:
        payload.pop("chat_template_kwargs", None)
    if effort is not None:
        payload["reasoning"] = {"effort": effort}
    elif thinking is not None:
        payload["reasoning"] = {"enabled": thinking}
    if payload.get("stream"):
        payload["stream_options"] = {"include_usage": True}
