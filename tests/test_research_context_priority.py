"""Visible correction evidence survives older findings without starving questions."""

import json
from copy import deepcopy
from dataclasses import replace

import pytest

from codey.operations.task_session import TaskSession
from codey.policies.task_policy import TaskPolicy
from codey.research.context_view import build_research_context_view
from codey.research.ledger import EvidenceItem, ResearchLedger
from codey.research.reference import ResearchReference
from codey.runtime.core.models import ToolCall, ToolResult


def populated(question_refs):
    ledger = ResearchLedger()
    ledger.questions = tuple(f"Question {n}: keep its complete conditions." for n in range(1, 9))
    for n, question in enumerate(question_refs, 1):
        ledger.references[f"f{n}"] = ResearchReference(f"claim-{n}", 1, "claim", "current_run",
            f"note-{n}", question_ref=question)
        ledger.evidence_items.append(EvidenceItem(f"Local claim {n}. " + "condition " * 100,
            "https://example.org/", f"Original evidence {n}. " + "exception " * 100, note_id=f"note-{n}"))
    session = TaskSession(TaskPolicy(frozenset({"control"}), strict_research=True))
    return session, ledger


@pytest.mark.parametrize("budget", [4000, 14000])
def test_twenty_findings_keep_latest_text_evidence_and_full_index_without_mutation(budget):
    session, ledger = populated(["q1"] * 20)
    original = deepcopy(ledger.references)
    view = build_research_context_view(session, ledger, budget=budget)
    assert view.references[0]["ref"] == "f20"
    assert view.references[0]["text"].startswith("Local claim 20.")
    assert view.references[0]["excerpt"].startswith("Original evidence 20.")
    assert view.references[0]["display_truncated"] and view.references[0]["detail_available"]
    assert view.questions[0]["finding_refs"] == list(original)
    assert view.omitted_references == 20 - len(view.references) > 0
    assert ledger.references == original and len(view.render()) <= budget


def test_recent_correction_and_rejected_proposal_refs_are_visible_with_other_questions():
    session, ledger = populated(["q1"] * 20 + ["q2"] * 2 + ["q3"])
    session.last_done_args = {"report_sections": {"coverage": [
        {"question_ref": "q1", "findings": ["f1"]}]}}
    session._memory_results["corrected"] = ToolResult(
        ToolCall("save_finding", {"question_ref": "q1"}), ok=True,
        model_text="Saved f20", canonical={"research_reference_bindings": {
            ref: binding.to_dict() for ref, binding in ledger.references.items()},
            "knowledge_notes": {"created": ["note-20"], "updated": []}})
    view = build_research_context_view(session, ledger)
    visible = {row["ref"] for row in view.references}
    assert {"f20", "f1", "f22", "f23"} <= visible
    assert view.references[0]["ref"] == "f20"
    assert {row["question_ref"] for row in view.references[:3]} == {"q1", "q2", "q3"}
    assert "f1" in ledger.references  # Display does not retract a historical claim.


def test_current_question_gets_first_slot_but_other_questions_keep_a_detail_slot():
    session, ledger = populated(["q1"] * 12 + ["q2"] * 12 + ["q3"] * 12)
    session._memory_results["attempt"] = ToolResult(
        ToolCall("save_finding", {"question_ref": "q3"}), ok=False,
        model_text="ERROR: not saved; f999 is not a reference", canonical={"finding_ref": "f999"})
    view = build_research_context_view(session, ledger)
    assert view.references[0]["ref"] == "f36"
    assert {row["ref"] for row in view.references[:3]} == {"f12", "f24", "f36"}
    assert "f999" not in {row["ref"] for row in view.references}


def test_recent_explicit_read_can_reopen_an_older_binding_without_hiding_newest():
    session, ledger = populated(["q1"] * 20)
    session._memory_results["read"] = ToolResult(
        ToolCall("knowledge_read", {"id": "f2", "question_ref": "q1"}), ok=True, model_text="Read f2")
    view = build_research_context_view(session, ledger)
    assert [row["ref"] for row in view.references[:2]] == ["f2", "f20"]


def test_recent_read_by_object_identity_uses_exact_binding_and_never_display_text():
    session, ledger = populated(["q1"] * 20)
    session._memory_results["read"] = ToolResult(
        ToolCall("knowledge_read", {"id": "claim-2", "question_ref": "q1"}), ok=True,
        model_text="Mention of f1 is not the reference that was read.")
    view = build_research_context_view(session, ledger)
    assert [row["ref"] for row in view.references[:2]] == ["f2", "f20"]


def test_read_of_explicit_revision_does_not_prioritize_another_version():
    session, ledger = populated(["q1"] * 20)
    ledger.references["f3"] = replace(ledger.references["f2"], revision=2)
    session._memory_results["read"] = ToolResult(
        ToolCall("knowledge_read", {"id": "claim-2", "revision": 2, "question_ref": "q1"}),
        ok=True, model_text="Exact object version was read.")
    view = build_research_context_view(session, ledger)
    assert [row["ref"] for row in view.references[:2]] == ["f3", "f20"]
    session._memory_results["read"] = ToolResult(
        ToolCall("knowledge_read", {"id": "f2", "question_ref": "q1"}),
        ok=True, model_text="Pinned short reference was read.")
    view = build_research_context_view(session, ledger)
    assert [row["ref"] for row in view.references[:2]] == ["f2", "f20"]


def test_receipt_affecting_multiple_bindings_puts_latest_binding_first():
    session, ledger = populated(["q1"] * 20)
    ledger.references["f19"] = replace(ledger.references["f2"], revision=2)
    # Canonical JSON recovery may restore lexical key order: f19 before f2.
    ledger.references = dict(sorted(ledger.references.items()))
    session._memory_results["write"] = ToolResult(
        ToolCall("save_finding", {"question_ref": "q1"}), ok=True, model_text="Updated note.",
        canonical={"knowledge_notes": {"created": [], "updated": ["note-2"]}})
    view = build_research_context_view(session, ledger)
    assert view.references[0]["ref"] == "f19"


def test_tight_budget_marks_question_detail_omissions_and_retains_all_indexes():
    session, ledger = populated([f"q{n}" for n in range(1, 9)] * 3)
    view = build_research_context_view(session, ledger, budget=4000)
    assert len({row["question_ref"] for row in view.references}) < 8
    assert "Not every question" in view.budget_issue
    assert all(len(row["finding_refs"]) == 3 for row in view.questions)
    assert len(view.render()) <= 4000


@pytest.mark.usefixtures("public_source_dns")
def test_production_save_receipt_prioritizes_exact_pinned_claim_and_evidence(tmp_path):
    from tests.test_research_findings import TEXT, URL, execute, setup

    store, tools, session, delegate = setup(tmp_path)
    try:
        execute(delegate, tools, session, "open_url", {"url": URL})
        passage = next(iter(tools.ledger.read_passages))
        saved = [execute(delegate, tools, session, "save_finding", {
            "claim": claim, "passage_ref": passage, "question_ref": "q1"}) for claim in (
                "Writers and readers can run at the same time.",
                "There is only one writer at a time.", "Reading and writing can proceed concurrently.")]
        assert all(result.ok for result in saved)
        session._memory_results.clear()
        session._memory_results["recent"] = saved[0]
        view = build_research_context_view(session, tools.ledger, store, budget=4000, session_id="first")
        refs = [json.loads(result.model_text)["finding_ref"] for result in saved]
        assert [row["ref"] for row in view.references[:2]] == [refs[0], refs[-1]]
        assert view.references[0]["text"] == "Writers and readers can run at the same time."
        assert view.references[0]["excerpt"] == TEXT
        assert set(view.questions[0]["finding_refs"]) == set(refs)
        assert [row["ref"] for row in view.references] == [refs[0], refs[-1], refs[1]]
    finally:
        store.close()
