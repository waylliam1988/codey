"""Project real Hybrid research receipts without repeating execution."""
from __future__ import annotations

from pathlib import Path
from typing import Never

import pytest

from codey.app.headless_runner import HeadlessAppContext
from codey.knowledge.changes import KnowledgeChanges
from codey.operations import task_entry
from codey.operations.context import RunFrame, RunHooks, RunWork
from codey.operations.research_flow import research_payload
from codey.operations.result import ModeOutcome
from codey.operations.task_run import TaskRunDeps
from codey.operations.task_session import TaskSession
from codey.policies.task_policy import TaskPolicy
from codey.research.run_result import ResearchRunResult
from codey.research.tools import ResearchTools
from codey.runtime.observe.execution_evidence import ExecutionEvidence
from codey.types.json_value import decode_json_object
from tests.support.task_entry_helpers import entry_frame, submission


def _unexpected(*args: object, **kwargs: object) -> Never:
    raise AssertionError("Projection must not execute another task or review")


def _run(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, tools: ResearchTools) -> tuple[ModeOutcome, bool]:
    outcome = ModeOutcome({"type": "task_done", "summary": "done", "stop_reason": "done", "receipt": {}})

    def kernel(frame: RunFrame, work: RunWork, hooks: RunHooks, deps: TaskRunDeps,
               **kwargs: object) -> ModeOutcome:
        return outcome

    monkeypatch.setattr(task_entry, "run_entry_kernel", kernel)
    frame = entry_frame(submission("graph", project=None))
    frame.task_kind = "hybrid"
    frame.entry_session = TaskSession(TaskPolicy(frozenset({"control"})), task_kind="hybrid")
    frame.entry_research_tools = tools
    state = HeadlessAppContext(tmp_path / "state", port=0, emit_jsonl=lambda row: None)
    try:
        deps = TaskRunDeps(state=state, agent_run=_unexpected, collect_changes=_unexpected,
                          run_review=_unexpected, capture_provider_failure=_unexpected,
                          workspace_revisions=state.workspace_revisions)
        hooks = RunHooks(on_event=_unexpected, on_shell_request=_unexpected, update_checkpoint=_unexpected,
                         record_provider_failure=_unexpected, append_ledger=_unexpected,
                         provider_failover_order=lambda: (), supervisor=None)
        result = task_entry.run_task_mode(frame, RunWork([], ExecutionEvidence()), hooks, deps)
        recorded = state.research_changes.get(frame.run_id)
        if recorded is not None:
            assert recorded is tools.changes
        return result, recorded is not None
    finally:
        assert state.close()


def test_hybrid_completion_event_contains_research_payload(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    tools = ResearchTools(None, None, None)
    tools.ledger.record_search("graph", [{"url": "https://example.test", "title": "Graph"}])
    tools.created_ids.append("note-graph")
    tools.sources_read.add("https://example.test")
    result, recorded = _run(monkeypatch, tmp_path, tools)
    research = decode_json_object(result.event["research"])
    assert research["notes_created"] == ["note-graph"]
    assert research["queries"] == ["graph"]
    assert research["restoreable"] is False
    assert not recorded


@pytest.mark.parametrize("changed", [False, True])
def test_hybrid_completion_records_only_real_knowledge_changes(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, changed: bool,
) -> None:
    changes = KnowledgeChanges(tmp_path / "knowledge")
    changes.capture_content("notes/measurement.md", None)
    if changed:
        changes.record_content("notes/measurement.md", "A saved measurement.")
    result, recorded = _run(monkeypatch, tmp_path, ResearchTools(None, None, changes))
    assert recorded is changed
    assert decode_json_object(result.event["research"])["restoreable"] is changed


@pytest.mark.parametrize("restoreable", [False, True])
def test_research_payload_exposes_registered_restore_availability(restoreable: bool) -> None:
    result = ResearchRunResult(question="q", summary="done", stop_reason="done", turns=1,
                               synthesis_id="synthesis-1")
    payload = research_payload(result, restoreable=restoreable)
    assert payload["restoreable"] is restoreable
    assert payload["semantic_review"] is None
