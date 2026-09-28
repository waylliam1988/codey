"""Neutral text-arg helper for research tools (no loop dependency)."""
from __future__ import annotations


def first_text_arg(args: dict, key: str) -> str:
    value = args.get(key) if isinstance(args, dict) else None
    if isinstance(value, str) and value.strip():
        return value.strip()
    if isinstance(value, (list, tuple)):
        for item in value:
            text = str(item or "").strip()
            if text:
                return text
    if value not in (None, "") and not isinstance(value, (dict, list, tuple)):
        text = str(value).strip()
        if text:
            return text
    return ""


__all__ = ["first_text_arg"]
