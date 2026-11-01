"""Narrow negative checks for count questions diverted to coexistence.

These necessary lexical cues do not establish entailment, actor identity,
conditions or numerical correctness. Passing leaves interpretation unreviewed.
"""

from __future__ import annotations

import re

from codey.research.evidence_view import ResolvedReference
from codey.research.issues import ResearchIssue
from codey.research.report_proposal import ResearchReportProposal

_COUNT_QUESTION = re.compile(
    r"(?:只有|只能有|最多|至多)\s*(?:[一二三四五六七八九十两]|[0-9])|"
    r"单[\w\u3400-\u9fff]{1,8}(?:限制|约束)|"
    r"\b(?:only (?:be )?one|at most (?:one|two|three|\d+)|single[ -]\w+ (?:limit|restriction))\b", re.I)
_BOUND = re.compile(
    r"(?:只有|只能|仅有|仅限|最多|至多|至少|上限|下限).{0,16}(?:[一二三四五六七八九十两]|\d)|"
    r"单[\w\u3400-\u9fff]{1,8}(?:限制|约束)|"
    r"(?:不限制|不限|没有数量限制)|\b(?:only|at most|at least|no more than|no fewer than|up to|exactly)"
    r"\s+(?:(?:be|have|allow|permit)\s+)?(?:one|two|three|\d+)\b|"
    r"\bsingle[ -]\w+|\b(?:unlimited|no limit)\b", re.I)
_COEXISTENCE = re.compile(r"并发|同时|互不阻塞|不阻塞|\b(?:concurren\w*|simultaneous\w*)\b|"
                          r"at the same time|do(?:es)? not block", re.I)
_GAP = re.compile(r"尚未|未知|未明确|未证实|仍需|无法确定|未能|不能确定|"
                  r"\b(?:unresolved|unknown)\b|not (?:establish|determined|answer)", re.I)


def validate_answer_focus(proposal: ResearchReportProposal, resolved: tuple[ResolvedReference, ...],
                          questions: tuple[str, ...], *, local_submissions: bool = False) -> None:
    by_label = {item.label: item for item in resolved}
    for index, row in enumerate(proposal.coverage):
        if row["state"] not in {"evidence_found", "partial"}:
            continue
        question = questions[int(row["question_ref"][1:]) - 1]
        if not _COUNT_QUESTION.search(question):
            continue
        answer = row["answer"]
        answer_bound = bool(_BOUND.search(answer))
        # A related observation can be retained as partial, with an explicit gap.
        if not answer_bound and row["state"] == "partial" and _GAP.search(answer):
            continue
        excerpts = [evidence.evidence.value.content for label in row["findings"]
                    for evidence in by_label[label].evidence if evidence.relation == "supported_by"]
        basis = "\n".join(excerpts)
        missing_answer = not answer_bound and bool(_COEXISTENCE.search(answer))
        coexistence_only = bool(_COEXISTENCE.search(basis)) and not _BOUND.search(basis)
        missing_basis = answer_bound and coexistence_only
        if missing_answer or missing_basis:
            field = "findings" if proposal.findings_only else "answer"
            repair = ("Select a supported replacement finding in this existing coverage row. "
                      if proposal.findings_only else "Edit this existing coverage row. ")
            gap_repair = ("use partial; the application appends an unresolved-question notice. "
                          if proposal.findings_only else
                          "use partial and explicitly state that the requested limit remains unresolved. ")
            if local_submissions:
                repair = "Select a supported replacement only if direct evidence is available. "
                gap_repair = ("submit_research_answer with state=unresolved and findings=[] for this target. "
                              "This needs no new finding. Changing an unsupported selection to partial does not "
                              "supply the missing direct evidence. ")
            raise ResearchIssue("count_limit_not_addressed", f"report_sections.coverage[{index}].{field}",
                f"{row['question_ref']} asks: {question}\n"
                "Coexistence or nonblocking operations do not establish an exclusive count limit. "
                + ("The answer does not state the requested limit. " if missing_answer else
                   "The selected supporting excerpts describe concurrency but omit a direct count limit. ")
                + ("Read the direct limiting statement using an available reading action, if one can be found. "
                   if coexistence_only else "Use the selected direct limiting statement. ")
                + repair + "If only related evidence is available, " + gap_repair + "Do not add another qN. "
                "This check detects a missing cue; it does not certify a supported interpretation.",
                allowed_refs=tuple(row["findings"]), requires_acquisition=coexistence_only,
                allowed_actions=("read direct limiting evidence", "save supported replacement",
                                 "submit unresolved with findings=[]" if local_submissions else "select partial or unresolved"))
