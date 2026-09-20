"""Leaf guard helpers for bounded Research payloads."""

from __future__ import annotations

import re
from collections.abc import Iterable

from codey.policies.redaction import looks_prompt_visible_secret
from codey.utils.refs import clip, identifier

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
    return bounded_int(default if isinstance(value, bool) else value, 1, upper, default=int(default or 1))


def bounded_int(
    value: object,
    lower: int,
    upper: int,
    *,
    default: int | None = None,
) -> int:
    fallback = lower if default is None else default
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        parsed = int(fallback)
    return max(lower, min(upper, parsed))


def status_token(
    value: object,
    allowed: Iterable[str],
    *,
    default: str,
    limit: int = 40,
) -> str:
    text = identifier(value, limit).lower()
    allowed_set = set(allowed)
    return text if text in allowed_set else default


def identifier_schema_ok(value: object, limit: int, *, allow_empty: bool = True) -> bool:
    if not isinstance(value, str):
        return False
    if not value:
        return allow_empty
    return value == identifier(value, limit)


def clip_schema_ok(value: object, limit: int, *, allow_empty: bool = True) -> bool:
    if not isinstance(value, str):
        return False
    if not value:
        return allow_empty
    return value == clip(value, limit)


__all__ = [
    "bounded_int",
    "bounded_limit",
    "clip",
    "clip_schema_ok",
    "connector_id",
    "generated_ref",
    "identifier",
    "identifier_schema_ok",
    "safe_connector_id",
    "status_token",
    "valid_digest_ref",
]
