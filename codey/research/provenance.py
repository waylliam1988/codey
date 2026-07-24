"""Opened-source provenance checks for Research reports."""

from __future__ import annotations

import re
from urllib.parse import urlparse

_URL_RE = re.compile(r"https?://[^\s<>)\]）】`\"']+", re.IGNORECASE)
_DOMAIN_RE = re.compile(r"(?<!@)\b(?:[a-z0-9-]+\.)+[a-z]{2,}\b", re.IGNORECASE)
_GENERIC_HOST_LABELS = {
    "www",
    "com",
    "org",
    "net",
    "edu",
    "gov",
    "mil",
    "int",
    "news",
    "search",
    "bing",
    "google",
    "duckduckgo",
}


def provenance_problem(
    summary: str,
    *,
    opened_sources: set[str],
    search_result_urls: set[str],
    allow_search_result_mentions: bool = False,
) -> str | None:
    text = str(summary or "")
    search_hosts = {_source_host(url) for url in search_result_urls}
    search_hosts.discard("")
    unread_urls = [
        url
        for url in _urls_in_text(text)
        if url not in opened_sources
        and not (
            allow_search_result_mentions
            and (url in search_result_urls or _site_domain_was_opened(_source_host(url), search_hosts))
        )
    ]
    if unread_urls:
        return (
            "Final report cites URL(s) you did not open: "
            + ", ".join(unread_urls[:3])
            + ". Open them first, or remove those citations before calling done."
        )
    opened_hosts = {_source_host(url) for url in opened_sources}
    opened_hosts.discard("")
    unopened_domains = [
        domain
        for domain in _domains_in_text(text)
        if not _site_domain_was_opened(domain, opened_hosts)
        and not (
            allow_search_result_mentions
            and _site_domain_was_opened(domain, search_hosts)
        )
    ]
    if unopened_domains:
        return (
            "Final report names source domain(s) you did not open: "
            + ", ".join(unopened_domains[:3])
            + ". Open those pages first, or remove those source claims before calling done."
        )
    unopened_named_sources = (
        []
        if allow_search_result_mentions
        else _unopened_search_source_mentions(text, search_result_urls, opened_hosts)
    )
    if unopened_named_sources:
        return (
            "Final report names search result source(s) you did not open: "
            + ", ".join(unopened_named_sources[:3])
            + ". Open them first, or remove those source claims before calling done."
        )
    return None


def _urls_in_text(text: str) -> list[str]:
    urls = []
    for match in _URL_RE.findall(text):
        url = match.rstrip(".,;:，。；、)）】`\"'")
        if url:
            urls.append(url)
    return urls


def _domains_in_text(text: str) -> list[str]:
    domains: list[str] = []
    seen: set[str] = set()
    url_spans = [match.span() for match in _URL_RE.finditer(text)]
    for match in _DOMAIN_RE.finditer(text.lower()):
        if _overlaps(match.span(), url_spans):
            continue
        domain = match.group(0).strip(".").removeprefix("www.")
        if domain and domain not in seen:
            seen.add(domain)
            domains.append(domain)
    return domains


def _unopened_search_source_mentions(text: str, search_result_urls: set[str], opened_hosts: set[str]) -> list[str]:
    lower = text.lower()
    found: list[str] = []
    seen: set[str] = set()
    for url in search_result_urls:
        host = _source_host(url)
        if not host or _site_domain_was_opened(host, opened_hosts):
            continue
        if any(label in lower for label in _host_labels(host)):
            if host not in seen:
                seen.add(host)
                found.append(host)
    return found


def _host_labels(host: str) -> set[str]:
    labels = {host}
    parts = [part for part in host.split(".") if part and part not in _GENERIC_HOST_LABELS]
    labels.update(part for part in parts if len(part) >= 5)
    return labels


def _host_was_opened(host: str, opened_hosts: set[str]) -> bool:
    host = (host or "").lower().removeprefix("www.")
    if not host:
        return False
    return host in opened_hosts


def _site_domain_was_opened(host: str, opened_hosts: set[str]) -> bool:
    host = (host or "").lower().removeprefix("www.")
    if not host:
        return False
    return _host_was_opened(host, opened_hosts) or any(
        opened.endswith("." + host) for opened in opened_hosts
    )


def _overlaps(span: tuple[int, int], spans: list[tuple[int, int]]) -> bool:
    start, end = span
    return any(start < other_end and other_start < end for other_start, other_end in spans)


def _source_host(url: str) -> str:
    try:
        return (urlparse(str(url or "")).hostname or "").lower().removeprefix("www.")
    except Exception:
        return ""
