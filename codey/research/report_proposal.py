"""Typed model proposal; parsing does not read knowledge or render Markdown."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from codey.research.issues import ResearchIssue
from codey.reviews.report_sections import REQUIRED_SECTIONS, heading_key

FINDING_CITATION = re.compile(r"\[(f[1-9][0-9]*)\]")


@dataclass(frozen=True)
class ReportParagraph:
    path: str
    text: str
    refs: tuple[str, ...]


@dataclass(frozen=True)
class ResearchReportProposal:
    conclusion: ReportParagraph
    evidence: tuple[ReportParagraph, ...]
    counter: tuple[ReportParagraph, ...]
    source_quality: tuple[ReportParagraph, ...]
    coverage: tuple[dict[str, Any], ...]
    coverage_answers: tuple[ReportParagraph, ...]

    @property
    def paragraphs(self) -> tuple[ReportParagraph, ...]:
        return (self.conclusion, *self.evidence, *self.counter, *self.source_quality, *self.coverage_answers)

    @property
    def selected_refs(self) -> tuple[str, ...]:
        return tuple(dict.fromkeys([ref for row in self.paragraphs for ref in row.refs]
            + [ref for row in self.coverage for ref in row["findings"]]))


def parse_report_proposal(args: dict[str, Any]) -> ResearchReportProposal:
    raw_sections = args.get("report_sections")
    if not isinstance(raw_sections, dict) or set(raw_sections) != {"evidence", "counter", "source_quality", "coverage"}:
        raise ResearchIssue("report_structure", "report_sections", "requires exactly evidence, counter, source_quality, coverage")

    sections: dict[str, Any] = raw_sections

    def paragraph(path: str, text: str) -> ReportParagraph:
        if re.search(r"\[(?:\d+|s\d+)(?:\s*[,;][^\]]*)?\]", text):
            raise ResearchIssue("manual_citation", path, "use exact [fN] references; the application assigns source numbers")
        if re.search(r"\[f[^\]]*\]", FINDING_CITATION.sub("", text)):
            raise ResearchIssue("invalid_reference", path, "invalid finding citation; use [f1], [f2], etc.")
        return ReportParagraph(path, text, tuple(dict.fromkeys(FINDING_CITATION.findall(text))))

    summary = args["summary"]
    if any(heading_key(line) in REQUIRED_SECTIONS for line in summary.splitlines()) or re.search(
            r"\\n\s*#{1,6}\s+", summary):
        raise ResearchIssue("duplicate_report", "summary", "keep only the conclusion without report headings")
    def paragraphs(key: str) -> tuple[ReportParagraph, ...]:
        return tuple(paragraph(f"report_sections.{key}[{index}]", text) for index, text in enumerate(sections[key]))

    return ResearchReportProposal(paragraph("summary", summary), paragraphs("evidence"), paragraphs("counter"),
                                  paragraphs("source_quality"), tuple(dict(row) for row in sections["coverage"]),
                                  tuple(paragraph(f"report_sections.coverage[{index}].answer", row["answer"])
                                        for index, row in enumerate(sections["coverage"])))
