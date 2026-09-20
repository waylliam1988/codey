"""Single owner for Research URL normalization.

Four spellings used to live side by side: the ledger's open-table lookup
with a per-site ``or raw`` fallback, connector dedup keys, arxiv host
forcing, and redacted digest refs. Only the last one is a different concern
(it lives in ``identity.sanitize_research_url_ref``); everything else funnels
through here:

- :func:`parsed_url` -- fail-soft ``urlparse`` (unparseable input parses as
  empty instead of raising).
- :func:`canonical_key` / :func:`full_key` -- dedup keys
  (``scheme://host/path`` / plus ``?query``; ``www.`` stripped, trailing
  ``/`` stripped). Moved verbatim from ``connector_search``.
- :func:`opened_url` -- the ledger's open-table resolution with the one
  sanctioned fallback: ``canonical or stripped raw``. Every former
  ``ledger.canonical_opened_url(x) or ...`` site calls this, so the
  fallback can never drift per call site again.

Stdlib only (plus ``utils.refs`` for hostname shape): the ``glob
research/*.py`` toolchain/runtime ban and the ``source_domains`` stdlib-leaf
rule both stay satisfied.
"""

from __future__ import annotations

from typing import Any
from urllib.parse import ParseResult, urlparse

from codey.research import source_domains as _source_domains


def parsed_url(url: object) -> ParseResult:
    """Parse a URL, never raising: garbage parses as empty."""
    try:
        return urlparse(str(url or "").strip())
    except ValueError:
        return urlparse("")


def canonical_key(url: object) -> str:
    """Dedup key: ``scheme://host/path`` (``www.`` stripped)."""
    parsed = parsed_url(url)
    host = (parsed.hostname or "").lower().removeprefix("www.")
    path = (parsed.path or "").rstrip("/")
    return f"{parsed.scheme.lower()}://{host}{path}"


def full_key(url: object) -> str:
    """Dedup key keeping the query string."""
    parsed = parsed_url(url)
    host = (parsed.hostname or "").lower().removeprefix("www.")
    path = (parsed.path or "").rstrip("/")
    query = ("?" + parsed.query) if parsed.query else ""
    return f"{parsed.scheme.lower()}://{host}{path}{query}"


def host_key(url: object) -> str:
    """Normalized hostname for domain-table lookups (``www.`` stripped)."""
    return _source_domains.strip_www(parsed_url(url).hostname or "")


def opened_url(ledger: Any, url: object) -> str:
    """Resolve ``url`` through the ledger's open table, else stripped raw.

    ``ledger`` is duck-typed (only ``canonical_opened_url`` is used) so this
    module never imports the ledger: no import cycle, no behavior import.
    """
    text = str(url or "").strip()
    if not text:
        return ""
    try:
        canonical = ledger.canonical_opened_url(text)
    except (AttributeError, TypeError, ValueError):
        return text
    return str(canonical or "") or text


__all__ = [
    "canonical_key",
    "full_key",
    "host_key",
    "opened_url",
    "parsed_url",
]
