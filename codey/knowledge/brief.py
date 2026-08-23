"""Bounded Research-to-Project handoff."""

from __future__ import annotations

from dataclasses import dataclass

from codey.knowledge.store import KnowledgeStore
from codey.report_sections import parse_sections
from codey.refs import clip as _clip
from codey.text_budget import clip_middle


BRIEF_TOTAL_LIMIT = 6000
SECTION_ITEM_LIMIT = 5
SOURCE_LINE_LIMIT = 16
MAX_ITEM_CHARS = 220


@dataclass(frozen=True)
class ResearchBrief:
    """Structured view of one session's latest research synthesis.

    Sections are projected by the shared report parser instead of a local
    heading scanner, and no raw report body is carried: the writer gets the
    bounded sections it needs, never the whole vault.
    """

    synthesis_id: str = ""
    original_question: str = ""
    conclusions: tuple[str, ...] = ()
    counterpoints: tuple[str, ...] = ()
    evidence_urls: tuple[str, ...] = ()
    citation_map: tuple[str, ...] = ()
    source_quality_risks: tuple[str, ...] = ()
    evidence_items: tuple[str, ...] = ()
    coverage_notes: tuple[str, ...] = ()
    open_questions: tuple[str, ...] = ()

    def render(self) -> str:
        if not self.synthesis_id and not (
            self.conclusions or self.evidence_items or self.citation_map
        ):
            return ""
        lines = [
            "Research context from this chat:",
            f"- synthesis_id: {self.synthesis_id}" if self.synthesis_id else "",
            f"- original_question: {self.original_question}" if self.original_question else "",
        ]
        for title, items in (
            ("Key conclusions:", self.conclusions),
            ("Evidence items:", self.evidence_items),
            ("Citation map:", self.citation_map),
            ("Counter-evidence / limitations:", self.counterpoints),
            ("Source quality risks:", self.source_quality_risks),
            ("Search coverage:", self.coverage_notes),
            ("Evidence URLs:", self.evidence_urls),
            ("Open questions:", self.open_questions),
        ):
            if items:
                lines.append(title)
                lines.extend(f"- {item}" for item in items)
        lines.extend((
            "",
            "Use this as background only. Verify against project files before editing.",
        ))
        rendered, _truncated = clip_middle(
            "\n".join(line for line in lines if line),
            BRIEF_TOTAL_LIMIT,
        )
        return rendered


class KnowledgeBriefBuilder:
    def __init__(self, store: KnowledgeStore | None) -> None:
        self.store = store

    def build_for_session(self, session_id: str) -> ResearchBrief:
        if self.store is None or not session_id:
            return ResearchBrief()
        try:
            rows = self.store.index.recent(
                1,
                session_id=session_id,
                types=("synthesis", "decision"),
            )
        except (OSError, ValueError):
            return ResearchBrief()
        if not rows:
            return ResearchBrief()
        note = self.store.read_note(str(rows[0].get("id") or ""))
        if note is None:
            return ResearchBrief()
        sections = parse_sections(note.body)
        return ResearchBrief(
            synthesis_id=note.id,
            original_question=note.title,
            conclusions=_section_lines(sections.get("conclusion", "")),
            counterpoints=_section_lines(sections.get("counter", "")),
            evidence_urls=tuple(note.sources[:8]),
            citation_map=_section_lines(
                sections.get("sources", ""), limit=SOURCE_LINE_LIMIT, strip_bullets=False
            ),
            source_quality_risks=_section_lines(sections.get("source_quality", ""), limit=3),
            evidence_items=_section_lines(sections.get("evidence", "")),
            coverage_notes=_section_lines(sections.get("coverage", ""), limit=3),
            open_questions=tuple(note.open_questions[:5]),
        )


def _section_lines(
    text: str,
    *,
    limit: int = SECTION_ITEM_LIMIT,
    strip_bullets: bool = True,
) -> tuple[str, ...]:
    out: list[str] = []
    for line in str(text or "").splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        if strip_bullets and stripped.startswith(("- ", "* ")):
            stripped = stripped[2:].strip()
        # Long lines are clipped, never dropped: with the raw excerpt gone
        # from the handoff, silently losing a long conclusion would hide
        # exactly the content the writer needs.
        stripped = _clip(stripped, MAX_ITEM_CHARS)
        if stripped.casefold() not in {row.casefold() for row in out}:
            out.append(stripped)
        if len(out) >= max(0, int(limit)):
            break
    return tuple(out)


__all__ = [
    "BRIEF_TOTAL_LIMIT",
    "KnowledgeBriefBuilder",
    "ResearchBrief",
]
