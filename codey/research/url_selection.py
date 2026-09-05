"""URL selection filters for Research source acquisition."""

from __future__ import annotations

from urllib.parse import urlparse


def source_candidate_skip_reason(url: str) -> str:
    """Return a short reason when a URL is too broad to fetch as evidence."""
    if is_root_landing_page_url(url):
        return "low_value_landing_page_url"
    return ""


def is_root_landing_page_url(url: str) -> bool:
    try:
        parsed = urlparse(str(url or "").strip())
    except ValueError:
        return False
    if parsed.scheme.lower() not in {"http", "https"}:
        return False
    if not parsed.netloc:
        return False
    path = (parsed.path or "").strip()
    if path not in {"", "/"}:
        return False
    return not (parsed.query or parsed.fragment)


__all__ = [
    "is_root_landing_page_url",
    "source_candidate_skip_reason",
]
