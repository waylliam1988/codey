"""Research-plan data and coverage validation over committed observations."""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol

from codey.policies.task_policy import TaskPolicy
from codey.reviews.research_coverage import CoverageRow, coverage_rows
from codey.types.json_value import ToolArgs

if TYPE_CHECKING:
    from codey.research.ledger import ResearchLedger


INVESTIGATION_TOOLS = frozenset({"web_search", "open_url", "open_result", "reopen_source", "open_hit",
                                 "source_search", "knowledge_search", "knowledge_read"})
QUESTION_BOUND_TOOLS = INVESTIGATION_TOOLS | {"save_finding", "save_inference"}


class QuestionSessionView(Protocol):
    @property
    def task_kind(self) -> str: ...

    @property
    def policy(self) -> TaskPolicy: ...

def requires_question_ref(session: QuestionSessionView) -> bool:
    from codey.research.answer_contract import whole_research

    if whole_research(session):
        return False
    return session.policy.strict_research or session.task_kind == "evidence_followup"


def question_ref(args: ToolArgs, ledger: ResearchLedger, *, required: bool = False) -> str:
    ref = args.get("question_ref", "")
    if ref == "" and not required:
        return ""
    if not isinstance(ref, str) or ref not in {f"q{n}" for n in range(1, len(ledger.questions) + 1)}:
        raise ValueError("question_ref must name a registered qN; call research_plan first and select the question explicitly")
    return ref


def question_plan(raw: object, previous: tuple[str, ...] = ()) -> tuple[str, ...]:
    if (not isinstance(raw, list) or not 1 <= len(raw) <= 8
            or any(not isinstance(text, str) or not 1 <= len(text.strip()) <= 800 for text in raw)
            or len(set(raw)) != len(raw)):
        raise ValueError("questions requires 1..8 distinct nonempty questions (at most 800 characters each)")
    questions = tuple(raw)
    if questions[:len(previous)] != previous:
        raise ValueError("Keep existing questions and their order; append new questions without replacing or dropping old ones")
    return questions


def validate_question_coverage(raw: object, ledger: ResearchLedger) -> list[CoverageRow]:
    if not ledger.questions:
        raise ValueError("Call research_plan first to register the questions; do not claim coverage without a question list")
    rows = coverage_rows(raw)
    expected = {f"q{index}" for index in range(1, len(ledger.questions) + 1)}
    observed = {row["question_ref"] for row in rows}
    if observed != expected:
        raise ValueError("report_sections.coverage must account for every registered question exactly once. Missing: "
                         + ", ".join(sorted(expected - observed)) + "; unknown: " + ", ".join(sorted(observed - expected))
                         + ". Use exactly these registered question ids: " + ", ".join(sorted(expected))
                         + ". Attach coverage task_refs to existing answers; "
                         "do not invent a question id in done.")
    missing = [row["question_ref"] for row in rows
               if row["state"] in {"evidence_found", "partial"} and not row["findings"]]
    if missing:
        raise ValueError("report_sections.coverage: " + ", ".join(missing)
            + " claim an answer without any saved/read finding. Next action: investigate the first missing question "
            "and save its direct evidence with save_finding, or state its specific unresolved gap with "
            "not_investigated (investigations=[]) / unanswered_after_search (select actual aN receipts). "
            "Gap states require findings=[]; the application generates their gap notice instead of publishing answer text. "
            "Changing evidence_found to partial alone does not fix missing evidence; do not repeat the same report.")
    for index, row in enumerate(rows):
        path = f"report_sections.coverage[{index}] ({row['question_ref']})"
        if row["state"] in {"not_investigated", "unanswered_after_search"} and row["findings"]:
            raise ValueError(path + ": gap states require findings=[]; use partial when giving a supported partial answer")
        if row["state"] == "not_investigated" and row["investigations"]:
            raise ValueError(path + ": not_investigated requires investigations=[]; otherwise describe the actual attempt and gap")
        if row["state"] == "unanswered_after_search" and not row["investigations"]:
            raise ValueError(path + ": unanswered_after_search needs actual investigations for this question; "
                             "use not_investigated if it was not investigated")
        for attempt in row["investigations"]:
            if attempt not in ledger.investigations:
                raise ValueError(f"report_sections.coverage[{index}].investigations: unknown investigation {attempt}")
            if ledger.investigations[attempt].get("question_ref") != row["question_ref"]:
                available = [label for label, receipt in ledger.investigations.items()
                             if receipt.get("question_ref") == row["question_ref"]]
                raise ValueError(f"{path}: investigation {attempt} belongs to another question; "
                    "actual receipts for this question: " + ", ".join(available)
                    + ". Select the relevant original receipt, or investigate with this question_ref.")
        for ref in row["findings"]:
            if ref not in ledger.references:
                raise ValueError(f"report_sections.coverage[{index}].findings: unknown finding {ref}")
            if ledger.references[ref].question_ref != row["question_ref"]:
                available = [label for label, binding in ledger.references.items() if binding.question_ref == row["question_ref"]]
                raise ValueError(f"{path}: finding {ref} belongs to another question ({ledger.references[ref].question_ref}); "
                    "read it explicitly for this question. Existing findings for this question: " + ", ".join(available)
                    + ". Use only actual successful finding_ref receipts; rejected saves allocate no fN.")
    return rows
