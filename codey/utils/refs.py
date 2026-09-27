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
import math
import re
from collections.abc import Iterable

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


def strict_nonnegative_int(value: object) -> int:
    """Strict nonnegative int: bools are 0, only finite floats and digit strings pass.

    Differs from :func:`nonnegative_int` (which maps ``True`` to ``1`` via
    ``int(value)`` and accepts ``"+12"`` but rejects ``"12.0"`` like strict).
    This is the shared strict copy previously duplicated in
    ``codey.policies.action`` and ``codey.runtime.core.models``: ``bool`` →
    ``0``, ``int`` → ``max(0, v)``, finite ``float`` → ``max(int(v), 0)``,
    digit-only ``str`` (after strip) → ``int``, everything else (including
    ``nan``/``inf``, signed/decimal strings, unicode digits such as ``"²"``
    whose ``isdigit()`` is true but ``int()`` raises, ``None``) → ``0``.
    """
    if isinstance(value, bool):
        return 0
    if isinstance(value, int):
        return max(value, 0)
    if isinstance(value, float) and math.isfinite(value):
        return max(int(value), 0)
    if isinstance(value, str) and value.strip().isdigit():
        try:
            return int(value.strip())
        except ValueError:
            return 0
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


_RESEARCH_PROOF_PREFIX = "research_proof:"
_HEX16 = frozenset("0123456789abcdef")


def research_proof_ref(value: object) -> str:
    """Validate a ``research_proof:<16 lowercase hex>`` ref, else ``""``.

    Shared by Ghost completion gating and Research proof review so the two
    judgments cannot drift. Inputs are coerced with ``str(value or "")``
    (inherited from both original validators) and stripped before validation,
    so non-string scalars fail closed; only values coercing to exactly the
    canonical shape pass. Fail-closed: uppercase hex, wrong length, and wrong
    prefix are all rejected.
    """
    text = str(value or "").strip()
    if not text.startswith(_RESEARCH_PROOF_PREFIX):
        return ""
    suffix = text.removeprefix(_RESEARCH_PROOF_PREFIX)
    if len(suffix) == 16 and all(ch in _HEX16 for ch in suffix):
        return text
    return ""


def _is_hex_64(value: str) -> bool:
    return len(value) == 64 and all(char in "0123456789abcdefABCDEF" for char in value)


_SHA256_HEX_RE = re.compile(r"^[0-9a-f]{64}$")


def clean_sha256_hex(value: object) -> str:
    """Clean a bare 64-char lowercase hex sha256 digest, else ``""``.

    Shared by AnalysisRun and Artifact lineage so the two projections cannot
    drift. Contract differs from ``valid_digest_ref`` (which requires the
    ``sha256:`` prefix) and from ``content_digest`` (which hashes); only exact
    bare digests pass, uppercased input is lowercased first.
    """
    text = str(value or "").strip().lower()
    return text if _SHA256_HEX_RE.fullmatch(text) else ""


__all__ = [
    "DEFAULT_REF_LIMIT",
    "bounded_refs",
    "clean_sha256_hex",
    "clip",
    "digest_json",
    "content_digest",
    "digest_text",
    "identifier",
    "is_valid_hostname",
    "nonnegative_int",
    "normalize_text",
    "research_proof_ref",
    "stable_ref",
    "strict_nonnegative_int",
]
