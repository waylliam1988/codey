"""Bounded Research-to-Project handoff."""

from __future__ import annotations

from dataclasses import dataclass, field

from codey.knowledge.store import KnowledgeStore
from codey.text_budget import clip_middle

BRIEF_BODY_LIMIT = 3600
BRIEF_TOTAL_LIMIT = 6000


@dataclass(frozen=True)
class ResearchBrief:
    synthesis_id: str = ""
    original_question: str = ""
    conclusions: tuple[str, ...] = ()
    counterpoints: tuple[str, ...] = ()
    evidence_urls: tuple[str, ...] = ()
    citation_map: tuple[str, ...] = ()
    source_quality_risks: tuple[str, ...] = ()
    evidence_items: tuple[str, ...] = ()
    risks: tuple[str, ...] = ()
    open_questions: tuple[str, ...] = ()
    source_note_ids: tuple[str, ...] = ()
    raw: str = ""
    related_note_ids: tuple[str, ...] = field(default_factory=tuple)

    def render(self) -> str:
        if not self.synthesis_id and not self.raw:
            return ""
        lines = [
            "Research context from this chat:",
            f"- synthesis_id: {self.synthesis_id}" if self.synthesis_id else "",
            f"- original_question: {self.original_question}" if self.original_question else "",
        ]
        if self.conclusions:
            lines.append("Key conclusions:")
            lines.extend(f"- {item}" for item in self.conclusions)
        if self.evidence_urls:
            lines.append("Evidence URLs:")
            lines.extend(f"- {item}" for item in self.evidence_urls)
        if self.citation_map:
            lines.append("Citation map:")
            lines.extend(f"- {item}" for item in self.citation_map)
        if self.evidence_items:
            lines.append("Evidence items:")
            lines.extend(f"- {item}" for item in self.evidence_items)
        if self.counterpoints:
            lines.append("Counter-evidence / limitations:")
            lines.extend(f"- {item}" for item in self.counterpoints)
        if self.source_quality_risks:
            lines.append("Source quality risks:")
            lines.extend(f"- {item}" for item in self.source_quality_risks)
        if self.risks:
            lines.append("Risks:")
            lines.extend(f"- {item}" for item in self.risks)
        if self.open_questions:
            lines.append("Open questions:")
            lines.extend(f"- {item}" for item in self.open_questions)
        if self.related_note_ids:
            lines.append("Related note ids:")
            lines.extend(f"- {item}" for item in self.related_note_ids)
        if self.raw:
            excerpt, _truncated = clip_middle(self.raw, BRIEF_BODY_LIMIT)
            lines.extend(("", "Synthesis excerpt:", excerpt))
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
        try:
            links = self.store.index.links_for([note.id])
        except Exception:
            links = []
        related = tuple(str(link.get("dst_id") or "") for link in links if link.get("dst_id"))
        return ResearchBrief(
            synthesis_id=note.id,
            original_question=note.title,
            conclusions=_extract_section_lines(note.body, ("结论", "结论候选", "Key conclusions", "Conclusion")),
            counterpoints=_extract_section_lines(note.body, ("反证与限制", "反证", "Counter-evidence", "Counter", "Limitations")),
            evidence_urls=tuple(note.sources[:8]),
            citation_map=_extract_sources_section(note.body),
            source_quality_risks=_extract_section_lines(note.body, ("来源质量", "Source quality")),
            evidence_items=_extract_section_lines(note.body, ("关键证据", "Evidence", "Evidence Ledger")),
            risks=_extract_section_lines(note.body, ("风险", "Risks")),
            open_questions=_extract_section_lines(note.body, ("继续跟踪指标", "Open questions", "Questions")),
            source_note_ids=related,
            raw=note.body,
            related_note_ids=related,
        )


def _extract_section_lines(body: str, headings: tuple[str, ...], limit: int = 5) -> tuple[str, ...]:
    lines = [line.rstrip() for line in str(body or "").splitlines()]
    heading_lowers = tuple(item.lower() for item in headings)
    out: list[str] = []
    in_section = False
    for line in lines:
        stripped = line.strip()
        lower = stripped.strip("#:： ").lower()
        if stripped.startswith("#") or stripped.endswith((":", "：")):
            if any(item in lower for item in heading_lowers):
                in_section = True
                continue
            if in_section and out:
                break
        if not in_section:
            continue
        if stripped.startswith(("- ", "* ")):
            out.append(stripped[2:].strip())
        elif stripped and len(stripped) < 220:
            out.append(stripped)
        if len(out) >= limit:
            break
    return tuple(out)


def _extract_sources_section(body: str) -> tuple[str, ...]:
    lines = _extract_section_lines(body, ("来源", "Sources", "References"), limit=16)
    if lines:
        return lines
    out: list[str] = []
    for line in str(body or "").splitlines():
        stripped = line.strip()
        if stripped.startswith("[") and "]" in stripped:
            out.append(stripped)
    return tuple(out[:16])
