"""Small shared shape validators for Research boundary payloads."""

from __future__ import annotations

import re

from codey.redaction import looks_prompt_visible_secret


_SNAKE_RE = re.compile(r"^[a-z][a-z0-9]*(?:_[a-z0-9]+)*$")
def connector_id(value: object) -> str:
    text = str(value or "").strip().lower()
    return text if _SNAKE_RE.fullmatch(text) else ""


def safe_connector_id(value: object) -> str:
    text = str(value or "").strip()
    if not text or looks_prompt_visible_secret(text):
        return ""
    return connector_id(text)


def generated_ref(value: object, prefix: str) -> str:
    text = str(value or "").strip()
    safe_prefix = connector_id(prefix)
    marker = safe_prefix + ":"
    suffix = text.removeprefix(marker)
    if text.startswith(marker) and len(suffix) == 16 and all(ch in "0123456789abcdef" for ch in suffix):
        return text
    return ""


def valid_digest_ref(value: object) -> str:
    text = str(value or "").strip()
    suffix = text.removeprefix("sha256:")
    if text.startswith("sha256:") and len(suffix) == 64 and all(ch in "0123456789abcdef" for ch in suffix):
        return text
    return ""


def bounded_limit(value: object, *, default: int, upper: int) -> int:
    if isinstance(value, bool):
        return max(1, min(upper, int(default or 1)))
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        parsed = int(default or 1)
    return max(1, min(upper, parsed))


__all__ = [
    "bounded_limit",
    "connector_id",
    "valid_digest_ref",
    "generated_ref",
    "safe_connector_id",
]
