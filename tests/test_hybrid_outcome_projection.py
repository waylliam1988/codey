"""Hybrid display projects the kernel verdict without re-judging quality."""
from __future__ import annotations

import inspect

import pytest

from codey.operations import task_entry
from codey.operations.context import RunWork
from codey.operations.result import ModeOutcome
from codey.operations.task_session import TaskSession
from codey.policies.task_policy import TaskPolicy
from codey.research.run_result import ResearchRunResult
from codey.research.tools import ResearchTools
from codey.runtime.observe.execution_evidence import ExecutionEvidence
from codey.types.json_value import decode_json_object
from tests.support.task_entry_helpers import entry_frame, submission


def test_enrich_never_rejudges_quality() -> None:
    source = inspect.getsource(task_entry._enrich_hybrid_outcome)
    for forbidden in ("report_quality", "report_warnings", "review_report_quality", "Project review trigger"):
        assert forbidden not in source


@pytest.mark.parametrize("stop_reason", ["done", "no_progress", "max_turns"])
def test_enrich_preserves_stop_reason_and_projects_facts(stop_reason: str) -> None:
    session = TaskSession(TaskPolicy(frozenset({"control"})), task_kind="hybrid")
    revision = session.record_edit("a.py")
    session.record_verification("python -m unittest discover", revision, True, exit_code=0)
    outcome = ModeOutcome({"type": "task_done", "summary": "summary text", "stop_reason": stop_reason,
                           "receipt": {"display": {"summary": "summary text"}}})
    enriched = task_entry._enrich_hybrid_outcome(entry_frame(submission()), RunWork([], ExecutionEvidence()),
                                                outcome, session=session)
    assert enriched.event["stop_reason"] == stop_reason
    display = decode_json_object(decode_json_object(enriched.event["receipt"])["display"])
    assert "report_quality" not in display
    assert display["changed_files"] == ["a.py"]
    assert display["verification"] == "python -m unittest discover"
    assert "changed_files" not in decode_json_object(decode_json_object(outcome.event["receipt"])["display"])


def test_enrich_projects_hybrid_research_facts_with_note_ids() -> None:
    tools = ResearchTools(None, None, None)
    tools.ledger.record_search("alpha", [{"url": "https://example.test/a", "title": "A"}])
    tools.ledger.record_open("https://example.test/a", "https://example.test/a", "A", "Observed fact.")
    tools.created_ids.append("note-1")
    tools.updated_ids.append("note-2")
    tools.sources_read.add("https://example.test/a")
    tools.links_created = 1
    outcome = ModeOutcome({"type": "task_done", "summary": "summary text", "stop_reason": "done", "turns": 1})
    enriched = task_entry._enrich_hybrid_outcome(entry_frame(submission("alpha")),
                                                RunWork([], ExecutionEvidence()), outcome, research_tools=tools)
    research = decode_json_object(enriched.event["research"])
    assert research["notes_created"] == ["note-1"]
    assert research["notes_updated"] == ["note-2"]
    assert research["queries"] == ["alpha"]
    assert research["source_urls"] == ["https://example.test/a"]
    assert research["sources_read"] == 1
    assert "research" not in outcome.event


def test_enrich_preserves_metadata_and_explicit_synthesis_id() -> None:
    result = ResearchRunResult(question="q", summary="kept", stop_reason="done", turns=1)
    display: tuple[dict[str, object], ...] = ({"answer": "kept"},)
    outcome = ModeOutcome({"type": "task_done", "summary": "done", "receipt": {"display": {}}},
                          display=display, research_result=result)
    tools = ResearchTools(None, None, None)
    tools.synthesis_id = "20261014T175521-title-f375a657"
    tools.created_ids.append(tools.synthesis_id)
    enriched = task_entry._enrich_hybrid_outcome(entry_frame(submission("q")), RunWork([], ExecutionEvidence()),
                                                outcome, research_tools=tools)
    assert enriched.display == display
    assert enriched.research_result is result
    assert decode_json_object(enriched.event["research"])["synthesis_id"] == tools.synthesis_id
