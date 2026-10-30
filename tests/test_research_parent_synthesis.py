"""Acquisition patches are inputs to a new gated parent answer, not proofs."""

from dataclasses import replace

import pytest

from codey.operations.research_synthesis import synthesis_context
from codey.research.context import ResearchContext
from codey.research.ledger import EvidenceItem, ResearchLedger
from codey.research.pipeline import ResearchPipeline
from codey.research.run_result import ResearchRunResult
from codey.research.tools import ResearchTools
from codey.runtime.core.cancellation import TaskCancelled


def _finalize(pipeline, result, tools):
    return pipeline._finalize_pipeline_result(
        result, tools, planner_stop_reason="no_new_material", followup_rounds=0,
        total_fresh_sources=0, total_new_evidence=0, total_merged_evidence=0,
        total_attempted_fresh_sources=0, total_attempted_new_evidence=0)


def test_parent_reconsiders_gaps_even_when_investigation_saved_no_new_evidence():
    old = ResearchRunResult("compare cases", "old draft", "done", 1, completion_proof=object())
    tools = ResearchTools(search=None, store=None, changes=None)
    seen = []

    def synthesize(result, current_tools):
        seen.append((result, current_tools))
        return replace(result, summary="Unresolved comparison; final budget exhausted.",
                       stop_reason="max_turns", completion_proof=None)

    pipeline = ResearchPipeline(context=ResearchContext("compare cases", "s", "r"),
        run_iteration=lambda **_: None, search_factory=lambda: None, policy=None,
        synthesize_result=synthesize)
    output = _finalize(pipeline, old, tools)
    assert seen == [(old, tools)]
    assert output.final_result.stop_reason == "max_turns"
    assert output.final_result.completion_proof is None
    assert output.final_result.summary != old.summary


def test_parent_synthesis_cancellation_never_returns_successful_old_draft():
    def cancelled(*_):
        raise TaskCancelled("cancel final synthesis")

    pipeline = ResearchPipeline(context=ResearchContext("compare cases", "s", "r"),
        run_iteration=lambda **_: None, search_factory=lambda: None, policy=None,
        synthesize_result=cancelled)
    with pytest.raises(TaskCancelled):
        _finalize(pipeline, ResearchRunResult("compare cases", "old draft", "done", 1),
                  ResearchTools(search=None, store=None, changes=None))


def test_synthesis_handoff_is_bounded_and_keeps_source_identity_and_stance():
    ledger = ResearchLedger()
    for i in range(40):
        ledger.evidence_items.append(EvidenceItem(claim="claim " * 400, source_url=f"https://example.com/{i}",
            excerpt="quote " * 400, stance="context", note_id=f"note-{i}"))
    text = synthesis_context(ResearchTools(search=None, store=None, changes=None, ledger=ledger))
    assert len(text) < 17000
    assert "https://example.com/39" in text and "note-39" in text
    assert '"stance":"context"' in text and '"display_truncated":true' in text
    assert "every part of the original user question" in text
