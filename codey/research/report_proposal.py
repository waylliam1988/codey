"""Typed model proposal; parsing does not read knowledge or render Markdown."""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from codey.research.issues import ResearchIssue
from codey.reviews.report_sections import REQUIRED_SECTIONS, heading_key

if TYPE_CHECKING:
    from codey.research.evidence_view import ResolvedReference

FINDING_CITATION = re.compile(r"\[(f[1-9][0-9]*)\]")
_GAP_ANSWERS = {
    "not_investigated": "本轮未调查该问题，无法据此给出结论。",
    "unanswered_after_search": "本轮已记录调查尝试，但该问题仍未作答，无法据此给出结论。",
}
_PARTIAL_NOTICE = "该问题尚未完整回答；以上仅为已保存的局部发现。"


@dataclass(frozen=True)
class ReportParagraph:
    path: str
    text: str
    refs: tuple[str, ...]


def _paragraph(path: str, text: str) -> ReportParagraph:
    if re.search(r"\[(?:\d+|s\d+)(?:\s*[,;][^\]]*)?\]", text):
        raise ResearchIssue("manual_citation", path, "use exact [fN] references; the application assigns source numbers")
    if re.search(r"\[f[^\]]*\]", FINDING_CITATION.sub("", text)):
        raise ResearchIssue("invalid_reference", path, "invalid finding citation; use [f1], [f2], etc.")
    return ReportParagraph(path, text, tuple(dict.fromkeys(FINDING_CITATION.findall(text))))


@dataclass(frozen=True)
class ResearchReportProposal:
    conclusion: ReportParagraph
    evidence: tuple[ReportParagraph, ...]
    counter: tuple[ReportParagraph, ...]
    coverage: tuple[dict[str, Any], ...]
    coverage_answers: tuple[ReportParagraph, ...]
    answers_only: bool = False
    findings_only: bool = False

    @property
    def paragraphs(self) -> tuple[ReportParagraph, ...]:
        if self.answers_only:
            return self.coverage_answers
        return (self.conclusion, *self.evidence, *self.counter, *self.coverage_answers)

    @property
    def selected_refs(self) -> tuple[str, ...]:
        return tuple(dict.fromkeys([ref for row in self.paragraphs for ref in row.refs]
            + [ref for row in self.coverage for ref in row["findings"]]))


def _answers_proposal(coverage: tuple[dict[str, Any], ...], *, findings_only: bool = False,
                      unresolved_targets: dict[str, tuple[str, ...]] | None = None) -> ResearchReportProposal:
    # Gap states authorize no factual answer. Compile their text before any
    # report views or persisted bindings are formed. Original requests remain
    # untouched in their provider/execution receipts.
    coverage = tuple({**row, "answer": f"{row['question_ref']}：{_GAP_ANSWERS[row['state']]}"}
                     if row["state"] in _GAP_ANSWERS else row for row in coverage)
    coverage = tuple({**row, "answer": row["answer"] + "\n\n尚未完整回答的目标：" + "；".join(unresolved_targets[row["question_ref"]])}
                     if unresolved_targets and row["question_ref"] in unresolved_targets else row for row in coverage)
    answers = tuple(_paragraph(f"report_sections.coverage[{index}].answer", row["answer"])
                    for index, row in enumerate(coverage))
    conclusion = ReportParagraph("summary", "\n\n".join(
        row["answer"] + "".join(f" [{ref}]" for ref in row["findings"]
                               if ref not in FINDING_CITATION.findall(row["answer"]))
        for row in coverage), tuple(dict.fromkeys(ref for row in coverage for ref in row["findings"])))
    return ResearchReportProposal(conclusion, (), (), coverage, answers, answers_only=True, findings_only=findings_only)


def _selection_rows(raw: list[Any], investigations: Mapping[str, Mapping[str, Any]]) -> tuple[dict[str, Any], ...]:
    """Normalize a selection, without granting an evidence or completion verdict."""
    from codey.reviews.research_coverage import coverage_rows

    rows = []
    for index, row in enumerate(raw):
        required = {"question_ref", "state", "findings"}
        if (not isinstance(row, dict) or not required <= set(row)
                or set(row) - required - {"task_refs", "investigations"}):
            raise ResearchIssue("report_structure", f"report_sections.coverage[{index}]",
                "select question_ref, state, findings and task_refs; do not rewrite answer prose")
        attempts = row.get("investigations", [ref for ref, attempt in investigations.items()
            if attempt.get("question_ref") == row["question_ref"]] if row["state"] != "not_investigated" else [])
        rows.append({**row, "investigations": attempts, "answer": "待从所选发现的精确版本编译。"})
    return tuple(coverage_rows(rows))


def bind_selected_answers(proposal: ResearchReportProposal,
                          resolved: tuple[ResolvedReference, ...]) -> ResearchReportProposal:
    """Adopt exact pinned claims once; never ask a model to rewrite the report.

    Claim boundaries are saved findings, not sentences or punctuation. A saved
    conditional relationship stays intact, including multi-sentence conditions.
    """
    if not proposal.findings_only:
        return proposal
    by_label = {item.label: item for item in resolved}
    rows = []
    for row in proposal.coverage:
        if row["state"] in {"evidence_found", "partial"}:
            answer = "\n\n".join(by_label[label].version.value.content + f" [{label}]" for label in row["findings"])
            if row["state"] == "partial":
                answer += f"\n\n{row['question_ref']}：{_PARTIAL_NOTICE}"
            if len(answer) > 4000:
                raise ResearchIssue("report_selection_too_large", f"{row['question_ref']}.findings",
                    "selected findings exceed the 4000-character question answer limit; select fewer complete findings "
                    "or save a concise supported replacement without dropping its conditions", allowed_refs=tuple(row["findings"]),
                    allowed_actions=("reselect complete findings", "read exact original passages", "save supported concise replacement"))
            row = {**row, "answer": answer}
        rows.append(row)
    return _answers_proposal(tuple(rows), findings_only=True)


def bind_gap_investigations(proposal: ResearchReportProposal,
                            investigations: Mapping[str, Mapping[str, Any]]) -> ResearchReportProposal:
    """Project actual attempts for declared gaps, after validating submitted refs.

    An omitted receipt cannot turn an actual search into an uninvestigated
    question. Attempts do not provide evidence or an answer. Historical expanded
    proposals retain their original contract; archived reports are never rebuilt.
    """
    if not proposal.answers_only:
        return proposal
    coverage = []
    for row in proposal.coverage:
        if row["state"] in _GAP_ANSWERS or proposal.findings_only:
            refs = [ref for ref, attempt in investigations.items() if attempt.get("question_ref") == row["question_ref"]]
            row = {**row, "investigations": refs, "state": (
                "unanswered_after_search" if refs else "not_investigated") if row["state"] in _GAP_ANSWERS else row["state"]}
        coverage.append(row)
    return _answers_proposal(tuple(coverage), findings_only=proposal.findings_only)


def bind_unresolved_targets(proposal: ResearchReportProposal, targets: dict[str, tuple[str, ...]]) -> ResearchReportProposal:
    """Keep original local obligations visible in the single canonical answer."""
    if not targets:
        return proposal
    bound = _answers_proposal(proposal.coverage, findings_only=proposal.findings_only, unresolved_targets=targets)
    from codey.reviews.research_coverage import coverage_rows

    coverage_rows(list(bound.coverage))
    return bound


def report_character_budget(proposal: ResearchReportProposal, resolved: tuple[ResolvedReference, ...], task: str) -> dict[str, Any]:
    """Same canonical conclusion and exact claims for preview and admission."""
    from codey.research.task_requirements import conclusion_characters, report_requirements

    by_label = {item.label: item for item in resolved}
    contributions: list[dict[str, Any]] = [{"question_ref": row["question_ref"], "ref": ref,
                      "characters": conclusion_characters(by_label[ref].version.value.content)}
                     for row in proposal.coverage for ref in row["findings"]] if proposal.findings_only else []
    count = conclusion_characters(proposal.conclusion.text)
    return {"characters": count, "contributions": contributions,
            "notice_characters": count - sum(row["characters"] for row in contributions),
            "limits": [{"task_ref": row["task_ref"], **row["constraints"],
                        **({"remaining_characters": row["constraints"]["max_characters"] - count}
                           if "max_characters" in row["constraints"] else {})} for row in report_requirements(task)]}


def parse_report_proposal(args: dict[str, Any], *,
                          investigations: Mapping[str, Mapping[str, Any]] | None = None) -> ResearchReportProposal:
    raw_sections = args.get("report_sections")
    if isinstance(raw_sections, dict) and set(raw_sections) == {"coverage"}:
        if set(args) != {"report_sections"}:
            raise ResearchIssue("report_structure", "done", "submit answers only in report_sections.coverage")
        from codey.reviews.research_coverage import coverage_rows

        rows = raw_sections["coverage"]
        if isinstance(rows, list) and any(isinstance(row, dict) and "answer" not in row for row in rows):
            return _answers_proposal(_selection_rows(rows, investigations or {}), findings_only=True)
        # Old frozen snapshots keep their authored answers. New strict snapshots
        # expose the selection contract and reject answer before reaching here.
        return _answers_proposal(tuple(coverage_rows(raw_sections["coverage"])))
    # Original expanded proposals remain readable under an already frozen old
    # contract. New strict tool snapshots expose only the answers contract above.
    if not isinstance(raw_sections, dict) or set(raw_sections) != {"evidence", "counter", "coverage"}:
        raise ResearchIssue("report_structure", "report_sections", "requires exactly evidence, counter, coverage; source quality is generated from pinned metadata")

    sections: dict[str, Any] = raw_sections

    summary = args["summary"]
    if any(heading_key(line) in REQUIRED_SECTIONS for line in summary.splitlines()) or re.search(
            r"\\n\s*#{1,6}\s+", summary):
        raise ResearchIssue("duplicate_report", "summary", "keep only the conclusion without report headings")
    def paragraphs(key: str) -> tuple[ReportParagraph, ...]:
        return tuple(_paragraph(f"report_sections.{key}[{index}]", text) for index, text in enumerate(sections[key]))

    return ResearchReportProposal(_paragraph("summary", summary), paragraphs("evidence"), paragraphs("counter"),
                                  tuple(dict(row) for row in sections["coverage"]),
                                  tuple(_paragraph(f"report_sections.coverage[{index}].answer", row["answer"])
                                        for index, row in enumerate(sections["coverage"])))
