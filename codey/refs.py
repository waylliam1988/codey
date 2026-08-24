"""Shared bounded text/ref primitives for Codey's projections.

This module is the domain-neutral home of the bounded vocabulary that every
refs-only read model speaks: clipped identifiers, bounded ref tuples, and
content-addressed stable refs. It is a stdlib leaf: no I/O, no model calls,
and no imports from codey.

Research-flavored helpers that need project roots or URL semantics stay in
``codey.research.identity``; everything here is domain-independent.
"""

from __future__ import annotations

import hashlib
import json
import re
from typing import Iterable


DEFAULT_REF_LIMIT = 12


def clip(value: object, limit: int = 240) -> str:
    text = str(value or "").replace("\r\n", "\n").replace("\r", "\n").strip()
    if limit <= 0:
        return ""
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 3)].rstrip() + "..."


def normalize_text(value: object) -> str:
    return " ".join(str(value or "").split())


def nonnegative_int(value: object) -> int:
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        return 0


def identifier(value: object, limit: int = 120) -> str:
    text = clip(value, limit)
    return "".join(char if char.isalnum() or char in "._:-" else "_" for char in text)


def bounded_refs(values: Iterable[object], *, limit: int = DEFAULT_REF_LIMIT) -> tuple[str, ...]:
    refs: list[str] = []
    seen: set[str] = set()
    for value in values or ():
        text = identifier(value, 80)
        if not text or text in seen:
            continue
        refs.append(text)
        seen.add(text)
        if len(refs) >= limit:
            break
    return tuple(refs)


_HOSTNAME_LABEL_RE = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$")


def is_valid_hostname(value: object) -> bool:
    """True when the text is a well-formed DNS hostname.

    Fail-closed predicate for trust decisions: empty labels (``.gov``),
    doubled dots (``evil..gov``), leading/trailing hyphens, bare single
    labels, and oversized names are all invalid, so no suffix table can be
    talked into matching them.
    """

    lowered = normalize_text(value).lower()
    if not lowered or len(lowered) > 253 or "_" in lowered:
        return False
    labels = lowered.split(".")
    if len(labels) < 2:
        return False
    return all(_HOSTNAME_LABEL_RE.match(label) for label in labels)


def digest_text(value: object) -> str:
    return "sha256:" + hashlib.sha256(str(value or "").encode("utf-8")).hexdigest()


def digest_json(value: object) -> str:
    payload = json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":"))
    return digest_text(payload)


def content_digest(value: object) -> str:
    text = str(value or "").strip()
    suffix = text.removeprefix("sha256:")
    if text.startswith("sha256:") and _is_hex_64(suffix):
        return "sha256:" + suffix.lower()
    return digest_text(text)


def stable_ref(prefix: str, *parts: object) -> str:
    digest = digest_json([prefix, *parts]).removeprefix("sha256:")
    return f"{identifier(prefix, 40)}:{digest[:16]}"


def _is_hex_64(value: str) -> bool:
    return len(value) == 64 and all(char in "0123456789abcdefABCDEF" for char in value)


__all__ = [
    "DEFAULT_REF_LIMIT",
    "bounded_refs",
    "clip",
    "digest_json",
    "content_digest",
    "digest_text",
    "identifier",
    "is_valid_hostname",
    "nonnegative_int",
    "normalize_text",
    "stable_ref",
]
