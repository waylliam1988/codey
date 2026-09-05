"""Deterministic compiler for Research final report citations."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Mapping

from codey.utils.citation_scanner import (
    citation_ref_items,
    source_id_ref_items,
    source_id_refs,
)
from codey.utils.refs import clip
from codey.reviews.report_sections import REQUIRED_SECTIONS, parse_sections, section_title
from codey.research import report_quality
from codey.research.ledger import ResearchLedger
from codey.research.object_model import ResearchClaim, build_research_record

_PAGE_REF_SUFFIX = r"(?:\s+(?:p\.?|pp\.?|pages?|page)\s*\.?\s*\d+(?:\s*-\s*\d+)?)?"
_NUMERIC_REF_RE = re.compile(rf"(?<![A-Za-z0-9_!])\[(\d+)({_PAGE_REF_SUFFIX})\]", re.IGNORECASE)
_TABLE_SEPARATOR_RE = re.compile(r"^\s*\|?\s*:?-{3,}:?\s*(?:\|\s*:?-{3,}:?\s*)+\|?\s*$")
_NUMBERED_MARKDOWN_HEADING_RE = re.compile(
    r"^\s*(?:\*\*|__)\s*\d+[\.)、]\s*[^*_]{1,80}\s*(?:\*\*|__)\s*$"
)
_NO_STRONG_COUNTER_MARKERS = ("未找到强反证", "no strong counter", "no strong contrary")
_DELETE_UNSUPPORTED_MARKERS = (
    "早期识别",
    "及时治疗",
    "需要进一步研究",
    "需要更多研究",
    "future research",
    "further research",
    "highlights the importance",
    "underscores the importance",
)
_OPERATIONAL_MARKERS = (
    "ignore previous",
    "ignore the above",
    "忽略前",
    "忽略以上",
    "调用工具",
    "call tool",
    "\"tool\"",
)


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
    question: str = "",
    enforce_claim_support: bool = False,
) -> FinalizedAnswer:
    """Compile final citation numbers and the source table from saved evidence.

    The compiler is deliberately narrow: it rewrites existing numeric/source-id
    references and renders the final ``来源`` table from citable evidence. It
    never adds a new citation marker to an uncited claim.
    """

    text = str(answer or "")
    sections = parse_sections(text)
    if not sections:
        return FinalizedAnswer(text, reason="no_report_sections")
    citable_urls = _citable_urls(ledger)
    if not citable_urls:
        rendered = _render_report(sections)
        changed = _normalized_report(rendered) != _normalized_report(text)
        return FinalizedAnswer(
            rendered if changed else text,
            changed=changed,
            reason="no_citable_sources",
        )

    full_number_for_url = {url: index for index, url in enumerate(citable_urls, 1)}
    full_url_by_number = {number: url for url, number in full_number_for_url.items()}
    source_id_to_number = _source_id_numbers(source_ids or {}, ledger, full_number_for_url)
    old_number_to_new = _old_source_numbers(sections.get("sources", ""), ledger, full_number_for_url)
    if old_number_to_new is None:
        return FinalizedAnswer(
            text,
            source_count=len(citable_urls),
            reason="unmapped_numeric_refs",
        )
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
    for key in REQUIRED_SECTIONS:
        if key == "sources":
            continue
        body = sections.get(key, "")
        if not body.strip():
            continue
        # Old numeric refs must be interpreted before source-id refs become numbers.
        body = _rewrite_numeric_refs(body, numeric_ref_map)
        body = _rewrite_source_id_refs(body, source_id_to_number)
        compiled_bodies[key] = body

    filter_result = _filter_unsupported_required_claims(
        {**compiled_bodies, "sources": _render_sources(citable_urls, ledger)},
        ledger,
        question=question,
    ) if enforce_claim_support else _ClaimSupportFilterResult(compiled_bodies)
    compiled_bodies = {
        key: body
        for key, body in filter_result.sections.items()
        if key != "sources" and str(body or "").strip()
    }

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
        reason=_finalized_reason(changed, filter_result.changed),
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
) -> dict[int, int] | None:
    mapping: dict[int, int] = {}
    urls_by_number: dict[int, set[str]] = {}
    for citation in report_quality.parse_citation_rows(source_text, ledger):
        url = ledger.canonical_opened_url(citation.url) or citation.url
        if url in number_for_url:
            number = int(citation.number)
            urls = urls_by_number.setdefault(number, set())
            urls.add(url)
            if len(urls) > 1:
                return None
            mapping[number] = number_for_url[url]
    return mapping


def _numeric_ref_numbers(sections: Mapping[str, str]) -> tuple[int, ...]:
    refs: list[int] = []
    seen: set[int] = set()
    for key in REQUIRED_SECTIONS:
        if key == "sources":
            continue
        for item in citation_ref_items(sections.get(key, "")):
            if item.number in seen:
                continue
            seen.add(item.number)
            refs.append(item.number)
    return tuple(refs)


def _safe_numeric_ref_map(
    body_refs: tuple[int, ...],
    old_number_to_new: dict[int, int],
) -> dict[int, int] | None:
    # No silent inference: a body [n] that the 来源 table cannot explain is
    # an unmapped citation and must go back through repair, even when only
    # one citable source exists. Rewriting it here would silently assert
    # which source supports which claim.
    number_map = dict(old_number_to_new)
    for ref in body_refs:
        if ref not in number_map:
            return None
    return number_map


def _unmapped_source_id_refs(
    sections: Mapping[str, str],
    source_id_to_number: dict[str, int],
) -> tuple[str, ...]:
    refs = {
        ref
        for key, body in sections.items()
        if key != "sources"
        for ref in source_id_refs(body)
    }
    return tuple(sorted(ref for ref in refs if ref not in source_id_to_number))


def _rewrite_source_id_refs(text: str, source_id_to_number: dict[str, int]) -> str:
    result = text
    for item in reversed(source_id_ref_items(text)):
        number = source_id_to_number.get(item.source_id)
        if number is None:
            continue
        replacement = _source_id_replacement(text, item, number)
        result = result[:item.start] + replacement + result[item.end:]
    return result


def _source_id_replacement(text: str, item, number: int) -> str:
    replacement = f"[{number}{item.page_suffix}]" if item.bracketed else f"[{number}]"
    if item.start > 0 and re.match(r"[A-Za-z0-9_]", text[item.start - 1]):
        replacement = " " + replacement
    if item.end < len(text) and re.match(r"[A-Za-z0-9_]", text[item.end]):
        replacement = replacement + " "
    return replacement


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
        for item in citation_ref_items(body):
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
    for key in REQUIRED_SECTIONS:
        body = str(sections.get(key) or "").strip()
        if not body and key != "sources":
            continue
        parts.append(f"## {section_title(key)}\n{body}".rstrip())
    return "\n\n".join(parts).strip()


@dataclass(frozen=True)
class _ClaimSupportFilterResult:
    sections: dict[str, str]
    changed: bool = False


def _filter_unsupported_required_claims(
    sections: Mapping[str, str],
    ledger: ResearchLedger,
    *,
    question: str = "",
) -> _ClaimSupportFilterResult:
    """Remove required claims that cannot be bound to saved evidence.

    This is intentionally narrower than a report rewriter: it only moves or
    removes already-written lines after the citation compiler has mapped refs.
    It never invents a citation or attaches evidence to a claim.
    """

    candidate = _render_report(dict(sections))
    if not candidate:
        return _ClaimSupportFilterResult(dict(sections))
    try:
        record = build_research_record(
            question=question,
            summary=candidate,
            ledger=ledger,
            stop_reason="done",
        )
    except Exception:
        return _ClaimSupportFilterResult(dict(sections))
    claims_by_section = {
        section: [claim for claim in record.claims if claim.claim_section == section]
        for section in ("conclusion", "evidence")
    }
    claim_index = {section: 0 for section in claims_by_section}
    updated = dict(sections)
    changed = False
    downgraded: list[str] = []

    for section in ("conclusion", "evidence"):
        kept: list[str] = []
        for raw_line in str(sections.get(section, "") or "").splitlines():
            if not raw_line.strip():
                continue
            claim = _next_claim(claims_by_section, claim_index, section)
            if claim is None:
                kept.append(raw_line.rstrip())
                continue
            if claim.status == "evidence_backed" and claim.evidence_refs:
                kept.append(raw_line.rstrip())
                continue
            changed = True
            clean = str(claim.claim_text or "").strip()
            if _should_delete_unsupported_claim(clean):
                continue
            downgraded.append(_downgraded_claim_line(clean))
        updated[section] = "\n".join(dict.fromkeys(kept)).strip()

    if not citation_ref_items(updated.get("conclusion", "")):
        derived = _derive_conclusion_lines(updated.get("evidence", ""))
        if derived:
            updated["conclusion"] = "\n".join(derived)
            changed = True
    if not citation_ref_items(updated.get("evidence", "")):
        derived = _derive_evidence_lines(updated.get("conclusion", ""))
        if derived:
            updated["evidence"] = "\n".join(derived)
            changed = True

    if downgraded:
        counter_lines = _counter_lines(updated.get("counter", ""))
        if not citation_ref_items("\n".join(counter_lines)) and not _says_no_strong_counter(counter_lines):
            counter_lines.insert(0, "- 未找到强反证；以下事项缺少足够的已保存证据。")
        counter_lines.extend(downgraded)
        updated["counter"] = "\n".join(dict.fromkeys(counter_lines)).strip()

    return _ClaimSupportFilterResult(updated, changed=changed)


def _next_claim(
    claims_by_section: Mapping[str, list[ResearchClaim]],
    claim_index: dict[str, int],
    section: str,
) -> ResearchClaim | None:
    claims = claims_by_section.get(section) or []
    index = claim_index.get(section, 0)
    claim_index[section] = index + 1
    if index >= len(claims):
        return None
    return claims[index]


def _should_delete_unsupported_claim(text: str) -> bool:
    clean = re.sub(r"\s+", " ", str(text or "")).strip()
    if not clean:
        return True
    lower = clean.casefold()
    if _TABLE_SEPARATOR_RE.match(clean):
        return True
    if _NUMBERED_MARKDOWN_HEADING_RE.match(clean):
        return True
    if any(marker in lower for marker in _OPERATIONAL_MARKERS):
        return True
    if any(marker in lower for marker in _DELETE_UNSUPPORTED_MARKERS):
        return True
    return False


def _downgraded_claim_line(text: str) -> str:
    clean = re.sub(r"\s+", " ", str(text or "")).strip(" -•*")
    clean = re.sub(r"\[\d+(?:[^\]]*)?\]", "", clean).strip()
    return f"- 未能用已保存证据确认：{clip(clean, 260)}"


def _counter_lines(text: str) -> list[str]:
    return [
        line.rstrip()
        for line in str(text or "").splitlines()
        if line.strip()
    ]


def _says_no_strong_counter(lines: list[str]) -> bool:
    lower = "\n".join(lines).casefold()
    return any(marker in lower for marker in _NO_STRONG_COUNTER_MARKERS)


def _derive_conclusion_lines(evidence_text: str) -> list[str]:
    lines: list[str] = []
    for raw in str(evidence_text or "").splitlines():
        text = raw.strip()
        if not text:
            continue
        match = re.match(r"^[-*]?\s*\[(\d+(?:[^\]]*)?)\]\s*(.+?)\s*$", text)
        if match:
            number, body = match.groups()
            lines.append(f"- {body.strip()} [{number}]")
        elif citation_ref_items(text):
            lines.append(text)
        if len(lines) >= 3:
            break
    return list(dict.fromkeys(lines))


def _derive_evidence_lines(conclusion_text: str) -> list[str]:
    lines: list[str] = []
    for raw in str(conclusion_text or "").splitlines():
        text = raw.strip()
        if not text or not citation_ref_items(text):
            continue
        body = re.sub(r"\[\d+(?:[^\]]*)?\]", "", text)
        body = re.sub(r"^[-*]\s+", "", body).strip()
        if not body:
            continue
        prefix = "".join(match.group(0) for match in _NUMERIC_REF_RE.finditer(text))
        lines.append(f"- {prefix} {body}")
        if len(lines) >= 3:
            break
    return list(dict.fromkeys(lines))


def _finalized_reason(changed: bool, claim_filter_changed: bool) -> str:
    if not changed:
        return "already_compiled"
    if claim_filter_changed:
        return "claim_support_filtered"
    return "compiled_citations"


def render_research_report_sections(sections: Mapping[str, str]) -> str:
    """Render standard markdown report text from section mapping."""
    return _render_report(dict(sections))


def _normalized_report(value: str) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()


__all__ = [
    "FinalizedAnswer",
    "finalize_done_answer",
    "render_research_report_sections",
]
