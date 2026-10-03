"""Strict Research restores its ledger from real settled tool receipts."""
from dataclasses import replace
from types import SimpleNamespace

import pytest

from codey.operations.context import RunWork
from codey.operations.kernel_execution import execute_turn
from codey.operations.recovery import record_entry_policy, recover_effects_for_resume
from codey.operations.task_effects import KernelRecordedProvider
from codey.operations.task_entry import run_entry_kernel
from codey.operations.task_session import TaskSession
from codey.research.ledger import ResearchLedger
from codey.research.tools import ResearchToolOutput
from codey.runtime.core.models import ToolCall
from codey.runtime.observe.execution_evidence import ExecutionEvidence
from tests.stress.test_completion_truthful_oracle_real_run import _real_research_gate
from tests.test_auto_direct_answer_continues_to_kernel import _auto_frame, _SeqProvider
from tests.test_session_log_receipt_recovery_preserves_facts import _dirs, _open_runtime


@pytest.mark.parametrize("acknowledged", [False, True])
def test_restart_preserves_exact_research_ledger_without_refetch_or_write(tmp_path, monkeypatch, acknowledged):
    monkeypatch.setenv("NATIVE_TOOLS", "0")
    original, _, _, expected_ledger, report = _real_research_gate()
    project, state, logdir = _dirs(tmp_path)
    _, mutations, managed, _, deps, sink = _open_runtime(logdir, state, "s", "r", project)
    record_entry_policy(mutations, session_id="s", run_id="r", policy=original.policy)
    live = TaskSession(policy=original.policy, task_kind="research", project="", max_turns=8)
    ledger = ResearchLedger()
    url = next(iter(expected_ledger.final_url_set()))
    calls = []
    def search(query):
        calls.append("search")
        ledger.record_search(query, [{"title": "Helium article", "url": url, "snippet": "Helium supply."}])
        return ResearchToolOutput(f"1. Helium article\n   {url}", ok=True)
    def opened(actual, **kwargs):
        calls.append("open")
        ledger.record_open(actual, url, "Helium article", expected_ledger.source_text_for_url(url))
        return ResearchToolOutput(model_text="Helium article\nbody", ok=True)
    def write(args):
        calls.append("write")
        ledger.add_evidence_items(list(expected_ledger.evidence_items))
        return ResearchToolOutput("saved note-1", ok=True)
    tools = SimpleNamespace(ledger=ledger, web_search=search, open_url=opened, knowledge_write=write)
    for turn, call in enumerate([
        ToolCall("web_search", {"query": "helium supply"}),
        ToolCall("open_url", {"url": url}),
        ToolCall("knowledge_write", {"title": "Helium supply", "body": "Evidence", "sources": [url]}),
    ], 1):
        result = execute_turn(live, [call], run_id="r", turn=turn, effect_scope="task",
                              research_tools=tools, managed_outputs=managed,
                              session_id="s", intent_sink=sink)[0]
        assert result.ok is True, result.model_text
        if turn < 3 or acknowledged:
            KernelRecordedProvider(SimpleNamespace(send=lambda _: "ack"), sink).send("results")
    recovered = recover_effects_for_resume(deps, session_id="s", run_id="r", project="", task_kind="research")
    assert recovered.ok
    assert bool(recovered.recovered_tool_outcomes) is (not acknowledged)
    restored_tools = SimpleNamespace(ledger=ResearchLedger())
    monkeypatch.setattr("codey.operations.task_entry._entry_executors", lambda *args: (None, None, restored_tools))
    import json
    provider = _SeqProvider([json.dumps({"tool": "done", "args": {"summary": report}})])
    frame = _auto_frame("Research helium supply", provider, frame_run_id="r")
    frame.request = replace(frame.request, session_id="s", intent="research", max_turns=8)
    frame.task_kind = "research"
    frame.provider_id = "local"
    frame.recovered_tool_outcomes = recovered.recovered_tool_outcomes
    frame.settled_tool_outcomes = recovered.settled_tool_outcomes
    frame.recovered_tool_result_batch_id = recovered.recovered_tool_result_batch_id
    deps.state = SimpleNamespace()
    outcome = run_entry_kernel(frame, RunWork([], ExecutionEvidence()),
                               SimpleNamespace(on_event=lambda _: None, on_shell_request=None), deps,
                               task_kind="research")
    assert outcome.event["stop_reason"] == "done", outcome.event
    assert restored_tools.ledger.searches == ledger.searches
    assert restored_tools.ledger.opened_sources == ledger.opened_sources
    assert restored_tools.ledger.evidence_items == ledger.evidence_items
    assert restored_tools.ledger.source_text_for_url(url) == ledger.source_text_for_url(url)
    assert calls == ["search", "open", "write"]



def test_failed_ledger_projection_does_not_commit_partial_task_facts():
    from codey.agents.request import RecoveredToolOutcome
    from codey.operations.kernel_errors import RecoveryFailed
    from codey.operations.kernel_session_recovery import restore_task_session
    from codey.policies.task_policy import TaskPolicy
    from codey.toolchain.runtime import ToolOutcome

    policy = TaskPolicy(frozenset({"control", "web.read"}))
    session = TaskSession(policy=policy)
    ledger = ResearchLedger()
    rows = (
        RecoveredToolOutcome(ToolCall("web_search", {"query": "helium"}),
                             ToolOutcome("1. docs\n   https://example.com", True), 1, 0, effect_id="search"),
        RecoveredToolOutcome(ToolCall("open_url", {"url": "https://example.com"}),
                             ToolOutcome("source", True, canonical={"research_observation": {"bad": True}}),
                             2, 0, effect_id="open"),
    )
    frame = SimpleNamespace(run_id="r", recovered_tool_outcomes=(), settled_tool_outcomes=rows)
    with pytest.raises(RecoveryFailed, match="research ledger"):
        restore_task_session(frame, session, research_ledger=ledger)
    assert session.searches == []
    assert session.search_results == {}
    assert session.restored_effect_ids == set()
    assert ledger.final_url_set() == set()



def test_large_pdf_receipt_restores_exact_pages_and_evidence():
    import json

    from codey.research.ledger import EvidenceItem
    from codey.research.ledger_receipts import ledger_observation, restore_ledger_observation
    from codey.research.source_document import SourceDocument, SourcePage

    ledger = ResearchLedger()
    url = "https://example.com/report_(official).pdf"
    first = "First page: observed finding. " * 600
    second = "Second page: independent finding. " * 600
    document = SourceDocument(url, url, "Official report", "pdf", "application/pdf",
                              first + second, False, 2, (1, 2),
                              (SourcePage(1, first), SourcePage(2, second)))
    ledger.record_open_document(document)
    ledger.add_evidence_items([EvidenceItem("finding", url, second[:700], note_id="note-1", page=2)])
    restored = ResearchLedger()
    for kind in ("open_url", "knowledge_write"):
        receipt = json.loads(json.dumps(ledger_observation(ledger, kind, url=url)))
        assert len(json.dumps(receipt)) > (8000 if kind == "open_url" else 600)
        restore_ledger_observation(restored, receipt)
    assert restored.opened_sources == ledger.opened_sources
    assert restored.source_text_for_url(url) == first + second
    assert restored.source_pages_for_url(url) == {1: first, 2: second}
    assert restored.evidence_items == ledger.evidence_items
    assert restored.excerpt_in_source(url, second[:700], page=2)
