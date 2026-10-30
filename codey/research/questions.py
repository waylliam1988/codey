"""Research-plan data and coverage validation over committed observations."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from codey.reviews.research_coverage import coverage_rows

if TYPE_CHECKING:
    from codey.research.ledger import ResearchLedger


def question_plan(raw: object, previous: tuple[str, ...] = ()) -> tuple[str, ...]:
    if (not isinstance(raw, list) or not 1 <= len(raw) <= 8
            or any(not isinstance(text, str) or not 1 <= len(text.strip()) <= 800 for text in raw)
            or len(set(raw)) != len(raw)):
        raise ValueError("questions requires 1..8 distinct nonempty questions (at most 800 characters each)")
    questions = tuple(raw)
    if questions[:len(previous)] != previous:
        raise ValueError("Keep existing questions and their order; append new questions without replacing or dropping old ones")
    return questions


def validate_question_coverage(raw: object, ledger: ResearchLedger) -> list[dict[str, Any]]:
    if not ledger.questions:
        raise ValueError("Call research_plan first to register the questions; do not claim coverage without a question list")
    rows = coverage_rows(raw)
    expected = {f"q{index}" for index in range(1, len(ledger.questions) + 1)}
    observed = {row["question_ref"] for row in rows}
    if observed != expected:
        raise ValueError("report_sections.coverage must account for every registered question exactly once. Missing: "
                         + ", ".join(sorted(expected - observed)) + "; unknown: " + ", ".join(sorted(observed - expected)))
    for index, row in enumerate(rows):
        for attempt in row["investigations"]:
            if attempt not in ledger.investigations:
                raise ValueError(f"report_sections.coverage[{index}].investigations: unknown investigation {attempt}")
        for ref in row["findings"]:
            if ref not in ledger.finding_refs and ref not in ledger.knowledge_refs:
                raise ValueError(f"report_sections.coverage[{index}].findings: unknown finding {ref}")
    return rows
