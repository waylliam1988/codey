"""Neutral research run result (no loop dependency)."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class ResearchRunResult:
    question: str
    summary: str
    stop_reason: str
    turns: int
    queries: list[str] = field(default_factory=list)
    search_results: list[dict[str, Any]] = field(default_factory=list)
    opened_sources: list[dict[str, Any]] = field(default_factory=list)
    coverage: dict[str, Any] = field(default_factory=dict[str, Any])
    citation_map: list[dict[str, Any]] = field(default_factory=list)
    evidence_items: list[dict[str, Any]] = field(default_factory=list)
    counterpoints: list[str] = field(default_factory=list)
    quality_warnings: list[str] = field(default_factory=list)
    notes_created: list[str] = field(default_factory=list)
    notes_updated: list[str] = field(default_factory=list)
    links_created: int = 0
    sources_read: int = 0
    source_urls: list[str] = field(default_factory=list)
    synthesis_id: str = ""
    advisor_count: int = 0
    research_record: Any | None = None
    max_turns_used: int = 14
    # 共同 gate 的权威证明：投影与外层不得丢弃后另造。
    completion_proof: Any | None = None

    @property
    def receipt(self) -> str:
        return (
            f"{len(self.notes_created)} notes created, "
            f"{len(self.notes_updated)} notes updated, "
            f"{self.links_created} links, "
            f"{self.sources_read} sources read, "
            f"stop_reason={self.stop_reason}"
        )


__all__ = ["ResearchRunResult"]
