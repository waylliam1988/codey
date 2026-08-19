"""Deterministic compiler for Research final report citations."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Mapping

from codey.research import report_quality
from codey.research.ledger import ResearchLedger

_PAGE_REF_SUFFIX = r"(?:\s+(?:p\.?|pp\.?|pages?|page)\s*\.?\s*\d+(?:\s*-\s*\d+)?)?"
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
    unresolved_source_id_refs = _unmapped_source_id_refs(sections, source_id_to_number)
    if unresolved_source_id_refs:
        return FinalizedAnswer(
            text,
            source_count=len(citable_urls),
            reason="unmapped_source_id_refs",
        )
    body_numeric_refs = _numeric_ref_numbers(sections)
    numeric_ref_map = _safe_numeric_ref_map(body_numeric_refs, old_number_to_new)
    if numeric_ref_map is None:
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
        body = _rewrite_numeric_refs(body, numeric_ref_map)
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


def _numeric_ref_numbers(sections: Mapping[str, str]) -> tuple[int, ...]:
    refs: list[int] = []
    seen: set[int] = set()
    for key in report_quality.REQUIRED_SECTIONS:
        if key == "sources":
            continue
        for item in report_quality.citation_ref_items(sections.get(key, "")):
            if item.number in seen:
                continue
            seen.add(item.number)
            refs.append(item.number)
    return tuple(refs)


def _safe_numeric_ref_map(
    body_refs: tuple[int, ...],
    old_number_to_new: dict[int, int],
) -> dict[int, int] | None:
    number_map = dict(old_number_to_new)
    unresolved = tuple(ref for ref in body_refs if ref not in number_map)
    if not unresolved:
        return number_map
    inferred = _infer_unmapped_numeric_refs(unresolved, old_number_to_new)
    if not inferred:
        return None
    number_map.update(inferred)
    return number_map


def _infer_unmapped_numeric_refs(
    unresolved: tuple[int, ...],
    old_number_to_new: dict[int, int],
) -> dict[int, int]:
    targets = set(old_number_to_new.values())
    if len(targets) == 1:
        target = next(iter(targets))
        return {ref: target for ref in unresolved}
    return {}


def _unmapped_source_id_refs(
    sections: Mapping[str, str],
    source_id_to_number: dict[str, int],
) -> tuple[str, ...]:
    refs = {
        ref
        for key, body in sections.items()
        if key != "sources"
        for ref in report_quality.source_id_refs(body)
    }
    return tuple(sorted(ref for ref in refs if ref not in source_id_to_number))


def _rewrite_source_id_refs(text: str, source_id_to_number: dict[str, int]) -> str:
    result = text
    for item in reversed(report_quality.source_id_bracket_ref_items(text)):
        number = source_id_to_number.get(item.source_id)
        if number is None:
            continue
        result = result[:item.start] + f"[{number}{item.page_suffix}]" + result[item.end:]
    return result


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
    urls: list[str] = []
    seen: set[int] = set()
    for body in bodies.values():
        for item in report_quality.citation_ref_items(body):
            if item.number in seen:
                continue
            seen.add(item.number)
            url = url_by_number.get(item.number)
            if not url or url in urls:
                continue
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
