"""Single reader for the unified ``done`` text.

Coding historically used ``summary`` while Research used ``answer``. New
prompts document one name; old replies and stored runs keep working through
this compat reader. Stored payloads are never rewritten.
"""

from __future__ import annotations

from typing import Any

_DONE_TEXT_KEYS = ("summary", "answer", "message", "body", "text", "reason")


def read_done_text(args: Any) -> str:
    if not isinstance(args, dict):
        return ""
    for key in _DONE_TEXT_KEYS:
        value = args.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
        if isinstance(value, (list, tuple)):
            for item in value:
                text = str(item or "").strip()
                if text:
                    return text
        elif value not in (None, "") and not isinstance(value, (dict, set)):
            text = str(value).strip()
            if text:
                return text
    return ""


__all__ = ["read_done_text"]
