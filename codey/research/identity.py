"""Shared bounded identity helpers for Research projections."""

from __future__ import annotations

import hashlib
import json
import os
import re
from pathlib import Path
from typing import Iterable
from urllib.parse import urlparse, urlunparse


DEFAULT_REF_LIMIT = 12


def sanitize_research_url_ref(url: object) -> dict[str, object]:
    text = str(url or "").strip()
    if not text:
        return {}
    try:
        parsed = urlparse(text)
        host = (parsed.hostname or "").lower().removeprefix("www.")
    except ValueError:
        redacted, changed = _redacted_unparseable_url_for_digest(text)
        payload = {"url_digest": digest_text(redacted)}
        if changed:
            payload["redacted"] = True
        return payload
    if not host:
        redacted, changed = _redacted_unparseable_url_for_digest(text)
        payload = {"url_digest": digest_text(redacted)}
        if changed:
            payload["redacted"] = True
        return payload
    redacted_query = _redacted_query(parsed.query)
    query_redacted = bool(parsed.query)
    redacted = urlunparse((
        (parsed.scheme or "https").lower(),
        host,
        parsed.path or "",
        "",
        redacted_query,
        "",
    ))
    payload: dict[str, object] = {
        "url_digest": digest_text(redacted),
        "host": clip(host, 120),
    }
    if parsed.scheme:
        payload["scheme"] = identifier(parsed.scheme, 20)
    if parsed.username or parsed.password or query_redacted:
        payload["redacted"] = True
    if parsed.path:
        payload["path_digest"] = digest_text(parsed.path)
    return payload


def project_ref(project: str | Path | None) -> dict[str, object]:
    text = str(project or "").strip()
    if not text:
        return {}
    try:
        resolved = Path(text).expanduser().resolve()
        basename = resolved.name
        digest_source = os.path.normcase(str(resolved))
    except (OSError, RuntimeError, ValueError):
        path = Path(text)
        basename = path.name or clip(text, 80)
        digest_source = text
    return {
        "basename": clip(basename, 80),
        "digest": digest_text(digest_source),
    }


def path_ref(path: str | Path, *, project: str | Path | None = None) -> dict[str, object]:
    text = str(path or "").strip()
    if not text:
        return {}
    path_obj = Path(text)
    basename = path_obj.name or clip(text, 80)
    digest_source = text
    if project:
        try:
            root = Path(project).expanduser().resolve()
            resolved = (root / path_obj).resolve() if not path_obj.is_absolute() else path_obj.resolve()
            digest_source = os.path.relpath(resolved, root)
        except (OSError, RuntimeError, ValueError):
            digest_source = text
    return {
        "basename": clip(basename, 80),
        "digest": digest_text(os.path.normcase(digest_source)),
    }


def stable_ref(prefix: str, *parts: object) -> str:
    digest = digest_json([prefix, *parts]).removeprefix("sha256:")
    return f"{identifier(prefix, 40)}:{digest[:16]}"


def digest_json(value: object) -> str:
    payload = json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":"))
    return digest_text(payload)


def digest_text(value: object) -> str:
    return "sha256:" + hashlib.sha256(str(value or "").encode("utf-8")).hexdigest()


def digest_ref(value: object) -> str:
    text = str(value or "").strip()
    suffix = text.removeprefix("sha256:")
    if text.startswith("sha256:") and _is_hex_64(suffix):
        return "sha256:" + suffix.lower()
    return digest_text(text)


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


def nonnegative_int(value: object) -> int:
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        return 0


def normalize_text(value: object) -> str:
    return " ".join(str(value or "").split())


def clip(value: object, limit: int = 240) -> str:
    text = str(value or "").replace("\r\n", "\n").replace("\r", "\n").strip()
    if limit <= 0:
        return ""
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 3)].rstrip() + "..."


def _redacted_query(query: str) -> str:
    if str(query or ""):
        return "query=redacted"
    return ""


def _redacted_unparseable_url_for_digest(text: str) -> tuple[str, bool]:
    raw = str(text or "").strip()
    prefix = "<redacted-url>"
    match = re.match(r"^([A-Za-z][A-Za-z0-9+.-]*):", raw)
    if match:
        prefix = f"{match.group(1).casefold()}:<redacted-url>"
    elif raw.startswith("//"):
        prefix = "//<redacted-url>"
    if "?" not in raw:
        return prefix, True
    query = raw.split("?", 1)[1].split("#", 1)[0]
    redacted_query = _redacted_query(query) or "query=redacted"
    return f"{prefix}?{redacted_query}", True


def _is_hex_64(value: str) -> bool:
    return len(value) == 64 and all(char in "0123456789abcdefABCDEF" for char in value)


__all__ = [
    "DEFAULT_REF_LIMIT",
    "bounded_refs",
    "clip",
    "digest_json",
    "digest_ref",
    "digest_text",
    "identifier",
    "nonnegative_int",
    "normalize_text",
    "path_ref",
    "project_ref",
    "sanitize_research_url_ref",
    "stable_ref",
]
