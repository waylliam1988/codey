"""Research-flavored identity helpers: URLs, project roots, and paths.

Generic bounded text/ref primitives live in ``codey.utils.refs``; only the helpers
whose semantics depend on research inputs (URL sanitization, project-root
relative path digests) remain here.
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from urllib.parse import urlparse, urlunparse

from codey.utils.refs import clip, digest_text, identifier


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


__all__ = [
    "path_ref",
    "project_ref",
    "sanitize_research_url_ref",
]
