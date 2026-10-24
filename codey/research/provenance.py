"""Opened-source provenance checks for Research reports."""

from __future__ import annotations

import json
import re
from typing import Any
from urllib.parse import urlparse

_URL_RE = re.compile(r"https?://[^\s<>\]）】`\"']+", re.IGNORECASE)
_DOMAIN_RE = re.compile(r"(?<!@)\b(?:[a-z0-9-]+\.)+[a-z]{2,}\b", re.IGNORECASE)
_PATH_CONSTRUCTOR_RE = re.compile(
    r"\b(?:Path|PosixPath|WindowsPath|PurePath|PurePosixPath|PureWindowsPath)"
    r"\(\s*(?P<quote>['\"])(?P<path>[^'\"\r\n]+)(?P=quote)\s*\)"
)
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
_NON_SOURCE_DOTTED_IDENTIFIERS = {
    "os.path",
    "os.pathlike",
    "os.path.isreserved",
    "os.path.join",
    "os.path.abspath",
    "os.path.relpath",
    "os.scandir",
    "path.walk",
    "path.resolve",
    "purepath.anchor",
    "path.absolute",
    "path.move",
    "path.info",
    "pathlib.unsupportedoperation",
}


def _citation_text(text: str) -> str:
    """JSON arrays/numbers are not bracket citations; scan string values only."""
    def strings(value: Any) -> list[str]:
        if isinstance(value, str):
            return [value]
        if isinstance(value, dict):
            return [text for item in value.values() for text in strings(item)]
        if isinstance(value, list):
            return [text for item in value for text in strings(item)]
        return []
    try:
        value = json.loads(text)
    except ValueError:
        return text
    return "\n".join(strings(value))


def citation_problem(summary: str, *, ledger: Any, source_ids: dict[str, str],
                     require_citation: bool) -> str | None:
    """Validate only contract-scoped citations against the committed ledger."""
    from codey.research.report_quality import parse_citation_rows
    from codey.utils.citation_scanner import citation_ref_items, source_id_ref_items

    text = _citation_text(summary)
    urls = _urls_in_text(text)
    ids = source_id_ref_items(text)
    try:
        numeric = citation_ref_items(text)
    except ValueError:
        return "research_citation_page_invalid"
    if require_citation and not (urls or ids or numeric):
        return "research_citation_missing"
    numbers: dict[int, str] = {}
    for citation in parse_citation_rows(text, ledger):
        if citation.number < 1:
            return "research_citation_number_unknown"
        prior = numbers.get(citation.number)
        if prior is not None and prior != citation.url:
            return "research_citation_conflict"
        numbers[citation.number] = citation.url
    targets: list[tuple[str, tuple[int, ...]]] = [(url, ()) for url in urls]
    for ref in ids:
        url = source_ids.get(ref.source_id)
        if not url:
            return "research_citation_source_unknown"
        try:
            pages = citation_ref_items("[1" + ref.page_suffix + "]")[0].pages
        except ValueError:
            return "research_citation_page_invalid"
        targets.append((url, pages))
    for item in numeric:
        url = numbers.get(item.number)
        if not url:
            return "research_citation_number_unknown"
        targets.append((url, item.pages))
    for url, pages in targets:
        canonical = ledger.canonical_opened_url(url)
        if not canonical:
            return "research_citation_source_unread"
        if any(not ledger.page_was_read(canonical, page) for page in pages):
            return "research_citation_page_unread"
    return None


def provenance_problem(
    summary: str,
    *,
    opened_sources: set[str],
    search_result_urls: set[str],
    allow_search_result_mentions: bool = False,
) -> str | None:
    text = (summary or "")
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
        list[str]()
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
        url = _trim_url_boundary(match)
        if url:
            urls.append(url)
    return urls


def _trim_url_boundary(url: str) -> str:
    text = (url or "").strip().rstrip(".,;:，。；、】`\"'")
    while text.endswith(")") and text.count(")") > text.count("("):
        text = text[:-1].rstrip(".,;:，。；、】`\"'")
    return text


def _domains_in_text(text: str) -> list[str]:
    domains: list[str] = []
    seen: set[str] = set()
    url_spans = [match.span() for match in _URL_RE.finditer(text)]
    path_spans = [match.span("path") for match in _PATH_CONSTRUCTOR_RE.finditer(text)]
    for match in _DOMAIN_RE.finditer(text.lower()):
        if _overlaps(match.span(), url_spans) or _overlaps(match.span(), path_spans):
            continue
        domain = match.group(0).strip(".").removeprefix("www.")
        if domain in _NON_SOURCE_DOTTED_IDENTIFIERS:
            continue
        if domain and domain not in seen:
            seen.add(domain)
            domains.append(domain)
    return domains


def _unopened_search_source_mentions(text: str, search_result_urls: set[str], opened_hosts: set[str]) -> list[str]:
    lower = text.lower()
    opened_labels: set[str] = set()
    for opened_host in opened_hosts:
        opened_labels.update(_host_labels(opened_host))
    found: list[str] = []
    seen: set[str] = set()
    for url in search_result_urls:
        host = _source_host(url)
        if not host or _site_domain_was_opened(host, opened_hosts):
            continue
        if (host in lower or any(
            label not in opened_labels and _mentions_host_label(lower, label)
            for label in _host_labels(host)
            if label != host
        )) and host not in seen:
            seen.add(host)
            found.append(host)
    return found


def _mentions_host_label(lower_text: str, label: str) -> bool:
    if not label:
        return False
    if "." in label:
        return label in lower_text
    return re.search(rf"(?<![a-z0-9-]){re.escape(label)}(?![a-z0-9-])", lower_text) is not None


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
        return (urlparse(url or "").hostname or "").lower().removeprefix("www.")
    except Exception:
        return ""
