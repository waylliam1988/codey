"""Deterministic evidence review for Research final reports."""

from __future__ import annotations

import re
from dataclasses import dataclass
from urllib.parse import urlparse

_URL_RE = re.compile(r"https?://[^\s<>)\]]+", re.IGNORECASE)
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


@dataclass(frozen=True)
class EvidenceReviewResult:
    ok: bool
    message: str
    warnings: tuple[str, ...] = ()


def review_final_summary(
    summary: str,
    *,
    opened_sources: set[str],
    search_result_urls: set[str],
) -> EvidenceReviewResult:
    problem = provenance_problem(
        summary,
        opened_sources=opened_sources,
        search_result_urls=search_result_urls,
    )
    if problem:
        return EvidenceReviewResult(False, problem)
    warnings: list[str] = []
    if opened_sources and not _has_evidence_marker(summary):
        warnings.append("final report has opened sources but no explicit evidence/source section")
    return EvidenceReviewResult(True, "evidence review passed", tuple(warnings))


def provenance_problem(
    summary: str,
    *,
    opened_sources: set[str],
    search_result_urls: set[str],
) -> str | None:
    text = str(summary or "")
    unread_urls = [url for url in _urls_in_text(text) if url not in opened_sources]
    if unread_urls:
        return (
            "Final report cites URL(s) you did not open: "
            + ", ".join(unread_urls[:3])
            + ". Open them first, or remove those citations before calling done."
        )
    opened_hosts = {_source_host(url) for url in opened_sources}
    opened_hosts.discard("")
    unopened_domains = [domain for domain in _domains_in_text(text) if not _host_was_opened(domain, opened_hosts)]
    if unopened_domains:
        return (
            "Final report names source domain(s) you did not open: "
            + ", ".join(unopened_domains[:3])
            + ". Open those pages first, or remove those source claims before calling done."
        )
    unopened_named_sources = _unopened_search_source_mentions(text, search_result_urls, opened_hosts)
    if unopened_named_sources:
        return (
            "Final report names search result source(s) you did not open: "
            + ", ".join(unopened_named_sources[:3])
            + ". Open them first, or remove those source claims before calling done."
        )
    return None


def _has_evidence_marker(summary: str) -> bool:
    lower = str(summary or "").lower()
    return any(marker in lower for marker in ("关键证据", "evidence", "source", "sources", "来源"))


def _urls_in_text(text: str) -> list[str]:
    urls = []
    for match in _URL_RE.findall(text):
        url = match.rstrip(".,;:，。；、)")
        if url:
            urls.append(url)
    return urls


def _domains_in_text(text: str) -> list[str]:
    domains: list[str] = []
    seen: set[str] = set()
    for match in _DOMAIN_RE.findall(text.lower()):
        domain = match.strip(".").removeprefix("www.")
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
        if not host or _host_was_opened(host, opened_hosts):
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


def _source_host(url: str) -> str:
    try:
        return (urlparse(str(url or "")).hostname or "").lower().removeprefix("www.")
    except Exception:
        return ""
