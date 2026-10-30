"""Research continuation facts survive misleading narrative checkpoints."""

import json

from codey.operations.kernel_prompt import working_context
from codey.operations.task_guidance import task_working_context
from codey.operations.task_session import TaskSession
from codey.policies.task_policy import TaskPolicy
from codey.research.completion_guidance import render_research_observations
from codey.research.reference import ResearchReference
from codey.runtime.core.models import ToolCall, ToolResult


def state(session, ledger=None):
    return json.loads(render_research_observations(session, ledger).split("\n")[1])


def test_failed_writes_never_become_saved_references():
    session = TaskSession(TaskPolicy(frozenset({"control"}), strict_research=True))
    session._memory_results["failed-write"] = ToolResult(call=ToolCall("save_finding", {}), ok=False,
        model_text="ERROR: excerpt missing", canonical={"evidence_items": [{"claim": "not saved"}]})
    view = state(session)
    assert view["references"] == [] and view["saved_evidence_count"] == 0
    assert view["recent_results"] == [{"tool": "save_finding", "ok": False, "error": "ERROR: excerpt missing"}]


def test_view_reserves_questions_and_early_references_and_marks_omissions():
    from codey.research.context_view import build_research_context_view
    from codey.research.ledger import EvidenceItem, ResearchLedger

    ledger = ResearchLedger()
    ledger.questions = tuple("Question " + str(n) for n in range(8))
    for n in range(1, 60):
        ledger.references[f"f{n}"] = ResearchReference(f"long-object-id-{n}", 1, "claim", "current_run", f"long-note-id-{n}")
        ledger.evidence_items.append(EvidenceItem("claim " + str(n) + "x" * 5000,
            "https://example.org/", "excerpt", note_id=f"long-note-id-{n}"))
    session = TaskSession(TaskPolicy(frozenset({"control"}), strict_research=True))
    view = build_research_context_view(session, ledger, budget=4000)
    assert len(view.questions) == 8 and view.references[0]["ref"] == "f1"
    assert view.omitted_references > 0 and view.references[0]["display_truncated"]
    assert "long-note-id" not in view.render() and len(view.render()) <= 4000


def test_ordinary_context_and_evidence_only_contract():
    from codey.operations.kernel_protocol import build_turn_snapshot

    session = TaskSession(TaskPolicy(frozenset({"control"})))
    assert task_working_context(session) == working_context(session)
    session.task_kind = "evidence_followup"
    session.task_text = "Long parent question and obsolete report"
    text = task_working_context(session)
    assert "Evidence acquisition only" in text and session.task_text not in text
    assert "report_sections" not in text and "source_search" not in text
    done = next(spec for spec in build_turn_snapshot(session).frozen_specs if spec.name == "done")
    assert set(dict(done.parameters)) == {"summary"}


def test_parent_synthesis_projects_saved_ledger_once():
    from codey.operations.research_synthesis import synthesis_context
    from codey.research.ledger import EvidenceItem, ResearchLedger
    from codey.research.tools import ResearchTools

    ledger = ResearchLedger()
    ledger.questions = ("What was observed?",)
    ledger.references["f1"] = ResearchReference("object", 1, "claim", "current_run", "original-note")
    ledger.evidence_items.append(EvidenceItem("Earlier scoped finding", "https://example.org/",
        "Observed result", note_id="original-note"))
    session = TaskSession(TaskPolicy(frozenset({"control"}), strict_research=True), task_kind="research")
    text = task_working_context(session, ledger)
    view = state(session, ledger)
    assert view["saved_evidence_count"] == 1 and view["references"][0]["ref"] == "f1"
    assert view["references"][0]["interpretation_review"] == "unreviewed"
    assert view["recent_results"] == [] and "original-note" not in text
    handoff = synthesis_context(ResearchTools(None, None, None, ledger=ledger))
    assert "Earlier scoped finding" not in handoff and "What was observed?" not in handoff
    assert (text + handoff).count("Earlier scoped finding") == 1


def test_unknown_property_repair_allows_correcting_keys_without_rewriting_source_values():
    from codey.operations.kernel_prompt import _repair_prompt
    from codey.operations.kernel_protocol import build_turn_snapshot
    from codey.runtime.core.models import ToolPlan

    session = TaskSession(TaskPolicy(frozenset({"control", "knowledge.write"})), task_kind="evidence_followup")
    for native in (False, True):
        for error in ("knowledge_write unexpected arg ''", "knowledge_write arg 'evidence[0]' unexpected property ''"):
            prompt = _repair_prompt(ToolPlan(calls=[], control=None, protocol_error=error,
                protocol_tool_name="knowledge_write"), snapshot=build_turn_snapshot(session, native=native), native=native)
            assert "Remove the rejected property key at the reported location" in prompt
            assert "Preserve valid source excerpts" in prompt
            assert "the rejected key must change" in prompt




def test_valid_full_length_questions_cannot_exceed_serialization_budget():
    from codey.research.context_view import build_research_context_view
    from codey.research.ledger import ResearchLedger

    session = TaskSession(TaskPolicy(frozenset({"control"}), strict_research=True))
    ledger = ResearchLedger()
    ledger.questions = tuple(str(index) + "q" * 799 for index in range(8))
    short = build_research_context_view(session, ledger, budget=4000)
    assert len(short.render()) <= 4000 and short.budget_issue
    assert all(row["question_omitted"] for row in short.questions)
    assert all("question" not in row for row in short.questions)
    full = build_research_context_view(session, ledger, budget=14000)
    assert not full.budget_issue
    assert [row["question"] for row in full.questions] == list(ledger.questions)
    assert len(full.render()) <= 14000
