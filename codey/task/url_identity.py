from __future__ import annotations

from urllib.parse import urlsplit, urlunsplit


def source_url_key(url: str) -> str:
    try:
        parts = urlsplit(url)
    except ValueError:
        # User/tool input is validated by the acquisition network policy.
        return url
    return urlunsplit((parts.scheme.lower(), parts.netloc.lower(), parts.path or "/", parts.query, ""))
