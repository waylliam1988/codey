"""Private read-only advisors for Research evidence review."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable, Mapping, Sequence

from codey.runtime import cancellation
from codey.providers import controls as provider_controls
from codey.agents.consensus import ConsensusAdvice, MAX_CONSENSUS_ADVISORS, advisor_ids
from codey.research.source_document import compact_pages

RESEARCH_ADVISOR_TIMEOUT = 60.0
MAX_EVIDENCE_PACK_CHARS = 12_000
MAX_RESEARCH_ADVICE_CHARS = 4_000
MAX_ADVISOR_SOURCE_URLS = 16
MAX_ADVISOR_NOTES = 12
MAX_NOTE_BODY_CHARS = 900


@dataclass(frozen=True)
class EvidenceNote:
    id: str
    type: str
    title: str
    body: str
    sources: tuple[str, ...] = ()


@dataclass(frozen=True)
class EvidencePack:
    question: str
    draft: str
    opened_urls: tuple[str, ...] = ()
    search_result_urls: tuple[str, ...] = ()
    citation_map: tuple[dict[str, object], ...] = ()
    evidence_items: tuple[dict[str, object], ...] = ()
    notes: tuple[EvidenceNote, ...] = ()
    coverage: dict[str, object] = field(default_factory=dict)
    session_id: str = ""
    project: str = ""
    warnings: tuple[str, ...] = field(default_factory=tuple)

    def render(self) -> str:
        lines = [
            "Research EvidencePack",
            "",
            "Question:",
            _clip(self.question, 2_000) or "(empty)",
            "",
            "Owner draft report:",
            _clip(self.draft, 4_000) or "(empty)",
            "",
            "Opened URLs:",
        ]
        if self.opened_urls:
            lines.extend(f"- {url}" for url in self.opened_urls[:MAX_ADVISOR_SOURCE_URLS])
        else:
            lines.append("- (none)")
        if self.search_result_urls:
            lines.extend(["", "Search result URLs (not proof unless also opened):"])
            lines.extend(f"- {url}" for url in self.search_result_urls[:MAX_ADVISOR_SOURCE_URLS])
        if self.citation_map:
            lines.extend(["", "Citation map:"])
            for item in self.citation_map[:MAX_ADVISOR_SOURCE_URLS]:
                number = item.get("number")
                title = item.get("title") or ""
                url = item.get("url") or ""
                quality = item.get("quality") or {}
                pages = compact_pages(item.get("pages") or ())
                quality_text = " · ".join(
                    part for part in (
                        f"p.{pages}" if pages else "",
                        str(quality.get("level") or ""),
                        str(quality.get("kind") or ""),
                        str(quality.get("freshness") or ""),
                        str(quality.get("independent_group") or ""),
                    ) if part
                )
                lines.append(f"- [{number}] {title} - {url}" + (f" ({quality_text})" if quality_text else ""))
        if self.evidence_items:
            lines.extend(["", "Evidence items:"])
            for item in self.evidence_items[:MAX_ADVISOR_NOTES]:
                claim = str(item.get("claim") or "")
                source_url = str(item.get("source_url") or "")
                excerpt = str(item.get("excerpt") or "")
                stance = str(item.get("stance") or "supports")
                locator = str(item.get("locator") or "")
                lines.extend((
                    f"- [{stance}] {claim}",
                    f"  source: {source_url}" + (f" {locator}" if locator else ""),
                    f"  excerpt: {_clip(excerpt, MAX_NOTE_BODY_CHARS)}",
                ))
        if self.notes:
            lines.extend(["", "Saved notes:"])
            for note in self.notes[:MAX_ADVISOR_NOTES]:
                lines.extend((
                    f"[{note.type}] {note.title} (id={note.id})",
                    "sources: " + (", ".join(note.sources) if note.sources else "(none)"),
                    _clip(note.body, MAX_NOTE_BODY_CHARS),
                    "",
                ))
        if self.coverage:
            lines.extend(["", "Coverage:"])
            queries = self.coverage.get("queries") or []
            if queries:
                lines.extend(f"- query: {item}" for item in list(queries)[:MAX_ADVISOR_SOURCE_URLS])
            skipped = self.coverage.get("skipped_results") or []
            if skipped:
                lines.append("Skipped results:")
                for item in list(skipped)[:MAX_ADVISOR_SOURCE_URLS]:
                    lines.append(
                        f"- {item.get('title') or item.get('url') or ''} "
                        f"({item.get('reason') or 'skipped'})"
                    )
        if self.warnings:
            lines.extend(["Warnings:", *[f"- {item}" for item in self.warnings]])
        return _clip("\n".join(lines), MAX_EVIDENCE_PACK_CHARS)


def render_research_advisor_prompt(pack: EvidencePack) -> str:
    return "\n".join((
        "You are a private read-only evidence advisor for a research run.",
        "You are not the acting researcher.",
        "You cannot call tools, browse, open URLs, edit files, or write notes.",
        "Use only the EvidencePack below.",
        "Look for unsupported claims, missing counter-evidence, stale-source risks, and gaps that should be checked before the final report is trusted.",
        "Be concise. Return only advisor notes for the acting researcher; do not write the final answer.",
        "Do not mention hidden advisors, voting, MoA, or consensus.",
        "",
        pack.render(),
    ))


def run_research_advisors(
    *,
    selected_provider_id: str,
    provider_ids: Sequence[str],
    provider_labels: Mapping[str, str],
    availability: Callable[[], Mapping[str, bool]],
    connect_existing: Callable[[str], object],
    pack: EvidencePack,
    clear_provider_session: Callable[[str], None] | None = None,
    max_advisors: int = MAX_CONSENSUS_ADVISORS,
) -> tuple[ConsensusAdvice, ...]:
    cancellation.check()
    try:
        statuses = dict(availability())
    except Exception:
        statuses = {}
    candidates = advisor_ids(
        selected_provider_id,
        statuses,
        provider_ids,
        max_advisors=max_advisors,
    )
    reports: list[ConsensusAdvice] = []
    prompt = render_research_advisor_prompt(pack)
    for advisor_id in candidates:
        cancellation.check()
        advisor = None
        try:
            advisor = connect_existing(advisor_id)
            if clear_provider_session is not None:
                clear_provider_session(advisor_id)
            advisor.new_chat()
            with provider_controls.suppress_assistance():
                text = advisor.send(prompt, timeout=RESEARCH_ADVISOR_TIMEOUT)
            clipped = _clip(text, MAX_RESEARCH_ADVICE_CHARS)
            if clipped:
                reports.append(ConsensusAdvice(
                    advisor_id,
                    provider_labels.get(advisor_id, advisor_id),
                    clipped,
                ))
        except cancellation.TaskCancelled:
            raise
        except Exception:
            continue
        finally:
            if advisor is not None:
                try:
                    advisor.close()
                except Exception:
                    pass
    return tuple(reports)


def _clip(value: object, limit: int) -> str:
    text = "" if value is None else str(value)
    text = text.replace("\r\n", "\n").replace("\r", "\n").strip()
    if len(text) <= limit:
        return text
    return text[:limit].rstrip() + "\n[truncated]"