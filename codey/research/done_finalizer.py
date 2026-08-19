"""Deterministic compiler for Research final report citations."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Mapping

from codey.research import report_quality
from codey.research.ledger import ResearchLedger

_PAGE_REF_SUFFIX = r"(?:\s+(?:p\.?|pp\.?|pages?|page)\s*\.?\s*\d+(?:\s*-\s*\d+)?)?"
_SOURCE_ID_REF_RE = re.compile(rf"\[\[?(s\d+)({_PAGE_REF_SUFFIX})\]\]?", re.IGNORECASE)
_NUMERIC_REF_RE = re.compile(rf"(?<![A-Za-z0-9_!])\[(\d+)({_PAGE_REF_SUFFIX})\]", re.IGNORECASE)


@dataclass(frozen=True)
class FinalizedAnswer:
    text: str
    changed: bool = False
    source_count: int = 0
    reason: str = ""


def finalize_done_answer(
    answer: str,
    ledger: ResearchLedger,
    *,
    source_ids: Mapping[str, str] | None = None,
) -> FinalizedAnswer:
    """Compile final citation numbers and the source table from saved evidence.

    The compiler is deliberately narrow: it rewrites existing numeric/source-id
    references and renders the final ``来源`` table from citable evidence. It
    never adds a new citation marker to an uncited claim.
    """

    text = str(answer or "")
    citable_urls = _citable_urls(ledger)
    if not citable_urls:
        return FinalizedAnswer(text, reason="no_citable_sources")
    sections = report_quality.parse_sections(text)
    if not sections:
        return FinalizedAnswer(text, reason="no_report_sections")

    full_number_for_url = {url: index for index, url in enumerate(citable_urls, 1)}
    full_url_by_number = {number: url for url, number in full_number_for_url.items()}
    source_id_to_number = _source_id_numbers(source_ids or {}, ledger, full_number_for_url)
    old_number_to_new = _old_source_numbers(sections.get("sources", ""), ledger, full_number_for_url)
    unresolved_numeric_refs = _unmapped_numeric_refs(sections, old_number_to_new)
    if unresolved_numeric_refs:
        return FinalizedAnswer(
            text,
            source_count=len(citable_urls),
            reason="unmapped_numeric_refs",
        )

    compiled_bodies: dict[str, str] = {}
    for key in report_quality.REQUIRED_SECTIONS:
        if key == "sources":
            continue
        body = sections.get(key, "")
        if not body.strip():
            continue
        # Old numeric refs must be interpreted before source-id refs become numbers.
        body = _rewrite_numeric_refs(body, old_number_to_new)
        body = _rewrite_source_id_refs(body, source_id_to_number)
        compiled_bodies[key] = body

    referenced_urls = _referenced_urls(compiled_bodies, full_url_by_number)
    if not referenced_urls:
        return FinalizedAnswer(
            text,
            source_count=len(citable_urls),
            reason="no_referenced_citable_sources",
        )

    compact_number_by_url = {url: index for index, url in enumerate(referenced_urls, 1)}
    compact_number_by_full_number = {
        full_number_for_url[url]: compact_number_by_url[url]
        for url in referenced_urls
    }
    rewritten = {
        key: _rewrite_numeric_refs(body, compact_number_by_full_number)
        for key, body in compiled_bodies.items()
    }
    rewritten["sources"] = _render_sources(referenced_urls, ledger)

    compiled = _render_report(rewritten)
    changed = _normalized_report(compiled) != _normalized_report(text)
    return FinalizedAnswer(
        compiled if changed else text,
        changed=changed,
        source_count=len(referenced_urls),
        reason="compiled_citations" if changed else "already_compiled",
    )


def _citable_urls(ledger: ResearchLedger) -> list[str]:
    final_urls = ledger.final_url_set()
    urls: list[str] = []
    for item in ledger.evidence_items:
        url = str(item.source_url or "").strip()
        if not str(item.excerpt or "").strip():
            continue
        canonical = ledger.canonical_opened_url(url) or url
        if canonical in final_urls and canonical not in urls:
            urls.append(canonical)
    return urls


def _source_id_numbers(
    source_ids: Mapping[str, str],
    ledger: ResearchLedger,
    number_for_url: dict[str, int],
) -> dict[str, int]:
    numbers: dict[str, int] = {}
    for raw_id, raw_url in source_ids.items():
        source_id = str(raw_id or "").strip().lower()
        if not source_id:
            continue
        url = str(raw_url or "").strip()
        canonical = ledger.canonical_opened_url(url) or url
        number = number_for_url.get(canonical)
        if number is not None:
            numbers[source_id] = number
    return numbers


def _old_source_numbers(
    source_text: str,
    ledger: ResearchLedger,
    number_for_url: dict[str, int],
) -> dict[int, int]:
    mapping: dict[int, int] = {}
    for citation in report_quality.parse_citations(source_text, ledger):
        url = ledger.canonical_opened_url(citation.url) or citation.url
        if url in number_for_url:
            mapping[int(citation.number)] = number_for_url[url]
    return mapping


def _unmapped_numeric_refs(
    sections: Mapping[str, str],
    old_number_to_new: dict[int, int],
) -> tuple[int, ...]:
    refs = {
        item.number
        for key, body in sections.items()
        if key != "sources"
        for item in report_quality.citation_ref_items(body)
    }
    return tuple(sorted(ref for ref in refs if ref not in old_number_to_new))


def _rewrite_source_id_refs(text: str, source_id_to_number: dict[str, int]) -> str:
    def repl(match: re.Match[str]) -> str:
        number = source_id_to_number.get(match.group(1).lower())
        return f"[{number}{match.group(2)}]" if number else match.group(0)

    return _SOURCE_ID_REF_RE.sub(repl, text)


def _rewrite_numeric_refs(
    text: str,
    number_map: dict[int, int],
) -> str:
    def repl(match: re.Match[str]) -> str:
        old_number = int(match.group(1))
        new_number = number_map.get(old_number)
        return f"[{new_number}{match.group(2)}]" if new_number is not None else match.group(0)

    return _NUMERIC_REF_RE.sub(repl, text)


def _referenced_urls(
    bodies: Mapping[str, str],
    url_by_number: dict[int, str],
) -> list[str]:
    referenced = {
        item.number
        for body in bodies.values()
        for item in report_quality.citation_ref_items(body)
    }
    urls: list[str] = []
    for number in sorted(referenced):
        url = url_by_number.get(number)
        if url and url not in urls:
            urls.append(url)
    return urls


def _render_sources(urls: list[str], ledger: ResearchLedger) -> str:
    lines: list[str] = []
    for index, url in enumerate(urls, 1):
        title = _source_title(ledger, url)
        lines.append(f"[{index}] {title} - {url}")
    return "\n".join(lines)


def _source_title(ledger: ResearchLedger, url: str) -> str:
    title = ledger.source_title(url).strip() or "Source"
    return re.sub(r"\s+", " ", title).strip(" -–—:：") or "Source"


def _render_report(sections: dict[str, str]) -> str:
    parts: list[str] = []
    for key in report_quality.REQUIRED_SECTIONS:
        body = str(sections.get(key) or "").strip()
        if not body and key != "sources":
            continue
        parts.append(f"## {report_quality.section_title(key)}\n{body}".rstrip())
    return "\n\n".join(parts).strip()


def _normalized_report(value: str) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()
