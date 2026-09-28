"""Neutral research synthesis helpers (no loop dependency)."""
from __future__ import annotations

from collections import Counter
from typing import Any


def synthesis_title(question: str, limit: int = 80) -> str:
    text = " ".join((question or "").split())
    if len(text) > limit:
        text = text[: limit - 1].rstrip() + "…"
    return text or "Research synthesis"


def run_concept_tags(store: Any, note_ids: list[str], limit: int = 5) -> list[str]:
    try:
        from codey.knowledge.concept_schema import normalize_concept
    except Exception:
        return []
    try:
        rows = store.index.tags_for([note_id for note_id in note_ids if note_id], active_only=True)
    except Exception:
        return []
    counts: Counter[str] = Counter()
    for row in rows or ():
        try:
            concept = normalize_concept(row.get("tag"))
        except Exception:
            concept = ""
        if concept:
            counts[concept] += 1
    return [concept for concept, _ in counts.most_common(limit)]


def _source_meta(item: dict, quality_text: str = "") -> str:
    parts: list[str] = []
    if quality_text:
        parts.append(quality_text)
    if str(item.get("content_kind") or "") == "pdf":
        parts.append("pdf")
        try:
            from codey.research.source_document import compact_pages
        except Exception:
            compact_pages = None  # type: ignore[assignment]
        try:
            pages = str(compact_pages(item.get("pages_read") or ()) or "") if callable(compact_pages) else ""
        except Exception:
            pages = ""
        try:
            page_count = int(item.get("page_count") or 0)
        except Exception:
            page_count = 0
        if pages:
            parts.append(f"pages {pages}/{page_count}" if page_count else f"pages {pages}")
        if item.get("truncated"):
            parts.append("truncated")
    return " · ".join(parts)


def _evidence_locator(item: dict) -> str:
    locator = str(item.get("locator") or "")
    return f" {locator}" if locator else ""


def _appendix_opened(ledger: Any, lines: list[str]) -> None:
    try:
        opened = ledger.opened_sources_payload()
    except Exception:
        opened = []
    if not opened:
        return
    lines.append("### Opened Sources")
    for index, item in enumerate(opened, 1):
        try:
            quality = item.get("quality") or {}
        except Exception:
            quality = {}
        try:
            quality_text = " · ".join(
                part for part in (
                    str(quality.get("level") or ""),
                    str(quality.get("kind") or ""),
                    str(quality.get("freshness") or ""),
                    str(quality.get("independent_group") or ""),
                ) if part
            )
        except Exception:
            quality_text = ""
        try:
            source_meta = _source_meta(item, quality_text)
        except Exception:
            source_meta = ""
        try:
            title = item.get("title") or item.get("final_url") or ""
            url = item.get("final_url") or ""
        except Exception:
            title, url = "", ""
        lines.append(f"- [{index}] {title} - {url}" + (f" ({source_meta})" if source_meta else ""))


def _appendix_evidence(ledger: Any, lines: list[str]) -> None:
    try:
        evidence = ledger.evidence_payload()
    except Exception:
        evidence = []
    if not evidence:
        return
    lines.append("### Evidence Items")
    for item in evidence:
        try:
            lines.extend((
                f"- [{item.get('stance') or 'supports'}] {item.get('claim') or ''}",
                f"  source: {item.get('source_url') or ''}{_evidence_locator(item)}",
                f"  excerpt: {item.get('excerpt') or ''}",
            ))
        except Exception:
            continue


def _appendix_coverage(ledger: Any, lines: list[str]) -> None:
    try:
        coverage = ledger.coverage_payload()
    except Exception:
        coverage = {}
    try:
        queries = coverage.get("queries") if isinstance(coverage, dict) else None
    except Exception:
        queries = None
    if not queries:
        return
    lines.append("### Search Coverage")
    for query in queries or []:
        lines.append(f"- query: {query}")
    try:
        skipped = coverage.get("skipped_results") or []
    except Exception:
        skipped = []
    if not skipped:
        return
    lines.append("  skipped:")
    for item in skipped[:8]:
        try:
            lines.append(f"  - {item.get('title') or item.get('url') or ''} ({item.get('reason') or 'skipped'})")
        except Exception:
            continue


def _ledger_appendix(ledger: Any) -> str:
    """Local evidence ledger rendering (no old runner dependency)."""
    if ledger is None:
        return ""
    try:
        lines = ["## Evidence Ledger"]
        _appendix_opened(ledger, lines)
        _appendix_evidence(ledger, lines)
        _appendix_coverage(ledger, lines)
        return "\n".join(lines).strip()
    except Exception:
        return ""


def synthesis_body(summary: str, ledger: Any) -> str:
    body = (summary or "").strip()
    try:
        appendix = str(_ledger_appendix(ledger) or "")
    except Exception:
        appendix = ""
    if not appendix:
        return body
    return f"{body}\n\n{appendix}".strip()


__all__ = ["run_concept_tags", "synthesis_body", "synthesis_title"]
