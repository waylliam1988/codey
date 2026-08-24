"""Neutral markdown section parser shared by research reports and handoffs.

This module owns the one true definition of a research report's structure:
which headings exist, what they are called in either language, and how a
body of text splits into named sections. It is a stdlib-only leaf -- no
codey imports, no I/O -- so both the research quality gate and the
knowledge-layer Writer handoff can consume it without the knowledge layer
reaching upward into the research package.
"""

from __future__ import annotations

import re


_HEADING_RE = re.compile(r"^\s{0,3}#{1,6}\s+(.+?)\s*$")
_LIST_PREFIX_RE = re.compile(r"^\s*(?:[-*+]\s+|\d+[.)、]\s+)")
_HEADING_NUMBER_RE = re.compile(
    r"^(?:"
    r"\d+(?:\.\d+)*"
    r"|[一二三四五六七八九十百千万]+"
    r"|[ivxlcdm]+"
    r")\s*[\.\)、)）:：、-]\s*",
    re.IGNORECASE,
)

SECTION_ALIASES: dict[str, tuple[str, ...]] = {
    "conclusion": ("结论", "关键结论", "conclusion", "key conclusions"),
    "evidence": ("关键证据", "evidence", "key evidence"),
    "counter": ("反证与限制", "反证", "限制", "counter-evidence", "counter", "limitations"),
    "risk": ("风险", "risks"),
    "notes": ("备注", "说明", "注", "notes"),
    "method": ("方法", "method", "methods"),
    "source_quality": ("来源质量", "source quality", "source assessment"),
    "coverage": ("搜索覆盖", "research coverage", "search coverage", "coverage"),
    "sources": ("来源", "参考文献", "sources", "references"),
}
REQUIRED_SECTIONS = (
    "conclusion",
    "evidence",
    "counter",
    "source_quality",
    "coverage",
    "sources",
)
_SECTION_KEY_ORDER = (
    "source_quality",
    "conclusion",
    "evidence",
    "counter",
    "risk",
    "notes",
    "method",
    "coverage",
    "sources",
)


def normalize_heading(value: str) -> str:
    text = str(value or "").strip().strip("#").strip()
    text = text.strip("*_`[]() ")
    while True:
        next_text = _HEADING_NUMBER_RE.sub("", text).strip()
        if next_text == text:
            break
        text = next_text
    text = text.rstrip(":：").strip()
    return re.sub(r"\s+", " ", text).lower()


def _build_normalized_aliases() -> dict[str, frozenset[str]]:
    return {
        key: frozenset(normalize_heading(item) for item in aliases)
        for key, aliases in SECTION_ALIASES.items()
    }


_NORMALIZED_ALIASES = _build_normalized_aliases()


def heading_key(line: str) -> str:
    """Map one markdown line to its canonical section key (or "")."""

    stripped = str(line or "").strip()
    match = _HEADING_RE.match(stripped)
    if match:
        title = match.group(1)
    else:
        title = stripped.rstrip(":：")
    title = normalize_heading(title)
    if not title:
        return ""
    for key in _SECTION_KEY_ORDER:
        if title in _NORMALIZED_ALIASES[key]:
            return key
    return ""


def is_boundary_line(line: str) -> bool:
    """True when a line starts a new section.

    Two boundary shapes exist:

    1. Any line whose title resolves through ``heading_key`` -- markdown
       headings, bare numbered headings (``1. Conclusion``, ``一、结论``),
       and aliased colon titles (``风险:``). These switch to that key.
    2. Unknown markdown headings, which route content to the dropped
       unknown bucket so custom templates cannot pollute known sections.

    Plain prose and list items are never boundaries; in particular a short
    lead-in like ``具体如下：`` does not cut the section it introduces.
    """

    stripped = str(line or "").strip()
    if not stripped:
        return False
    if _HEADING_RE.match(stripped):
        return True
    return bool(heading_key(stripped))


def parse_sections(text: str) -> dict[str, str]:
    """Split a report body into its named sections.

    Boundary lines whose title matches an alias switch to that section;
    markdown headings without an alias switch to the dropped unknown
    bucket. Unclassified prose can therefore never masquerade as a known
    section's conclusions.
    """

    sections: dict[str, list[str]] = {}
    current = ""
    for line in str(text or "").splitlines():
        stripped = line.strip()
        markdown_heading = bool(_HEADING_RE.match(stripped))
        # An alias hit always wins, even when the line looks like a numbered
        # list item: bare "1. Conclusion" / "一、结论" headings are
        # documented report shapes and must open their section.
        key = heading_key(line)
        if markdown_heading or key:
            current = key
            continue
        if current:
            sections.setdefault(current, []).append(line)
    return {key: "\n".join(value).strip() for key, value in sections.items() if key}


def missing_required_sections(sections: dict[str, str]) -> list[str]:
    return [label for label in REQUIRED_SECTIONS if not sections.get(label, "").strip()]


def section_title(key: str) -> str:
    return {
        "conclusion": "结论",
        "evidence": "关键证据",
        "counter": "反证与限制",
        "source_quality": "来源质量",
        "coverage": "搜索覆盖",
        "sources": "来源",
    }.get(key, key)


__all__ = [
    "REQUIRED_SECTIONS",
    "SECTION_ALIASES",
    "heading_key",
    "is_boundary_line",
    "missing_required_sections",
    "normalize_heading",
    "parse_sections",
    "section_title",
]
