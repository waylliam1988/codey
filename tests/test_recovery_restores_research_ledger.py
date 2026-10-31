"""Strict Research restores its ledger from real settled tool receipts."""
from dataclasses import replace
from types import SimpleNamespace

import pytest

from codey.operations.context import RunWork
from codey.operations.kernel_execution import execute_turn
from codey.operations.recovery import record_entry_policy
from codey.operations.task_entry import run_entry_kernel
from codey.operations.task_session import TaskSession
from codey.research.ledger import ResearchLedger
from codey.runtime.core.models import ToolCall
from codey.runtime.observe.execution_evidence import ExecutionEvidence
from tests.support.provider_actions import ProviderActions
from tests.support.receipt_fixtures import inspect_receipt_facts
from tests.support.workspace_conversation import TextConversation
from tests.test_chat_workspace_tools_and_answer_completion import workspace_frame
from tests.test_session_log_receipt_recovery_preserves_facts import _dirs, _open_runtime


@pytest.mark.parametrize("acknowledged", [False, True])
def test_restart_preserves_exact_research_ledger_without_refetch_or_write(tmp_path, monkeypatch, acknowledged, public_source_dns):
    import json

    from codey.knowledge.changes import KnowledgeChanges
    from codey.knowledge.store import KnowledgeStore
    from codey.policies.task_policy import TaskPolicy
    from codey.research.tools import ResearchTools
    from tests.support.research_script import answer_args

    monkeypatch.setenv("NATIVE_TOOLS", "0")
    project, state, logdir = _dirs(tmp_path)
    _, mutations, managed, _, deps, sink = _open_runtime(logdir, state, "s", "r", project, task_kind="research")
    policy = TaskPolicy(frozenset({"control", "web.read", "knowledge.read", "knowledge.write"}), strict_research=True)
    record_entry_policy(mutations, session_id="s", run_id="r", policy=policy, durable=sink.durable)
    live = TaskSession(policy, task_kind="research", task_text="Research helium supply", max_turns=8)
    url = "https://example.com/helium"
    calls = []

    class Search:
        def search(self, query, limit=8):
            calls.append("search")
            return [{"title": "Helium article", "url": url, "snippet": "Helium supply."}]

        def fetch(self, target):
            calls.append("open")
            return {"status": "ok", "url": target, "title": "Helium article",
                    "text": "Helium is separated from natural gas streams.", "truncated": False}

    store = KnowledgeStore(tmp_path / "knowledge")
    tools = ResearchTools(Search(), store, KnowledgeChanges(store.root), session_id="s")
    scripted_calls = [ToolCall("research_plan", {"questions": ["How is helium obtained?"]}),
                      ToolCall("web_search", {"query": "helium supply", "question_ref": "q1"}),
                      ToolCall("open_url", {"url": url, "question_ref": "q1"}),
                      ToolCall("save_finding", {})]
    try:
        for turn, call in enumerate(scripted_calls, 1):
            if call.name == "save_finding":
                call = ToolCall("save_finding", {"question_ref": "q1", "claim": "Helium supply depends on gas processing.",
                    "passage_ref": next(iter(tools.ledger.read_passages))})
            result = execute_turn(live, [call], run_id="r", turn=turn, effect_scope="task",
                research_tools=tools, managed_outputs=managed, session_id="s", intent_sink=sink)[0]
            assert result.ok, result.model_text
            if turn < 4 or acknowledged:
                from codey.operations.kernel_protocol import build_turn_snapshot

                sink.bind_request(build_turn_snapshot(live), logical_turn=turn)
                sink._request_context["effect_scope"] = "task"
                ProviderActions(SimpleNamespace(send=lambda _: "ack"), sink).send("results")
        ledger = tools.ledger
        assert len(ledger.references) == 1
        recovered = inspect_receipt_facts(deps, session_id="s", run_id="r", project="", task_kind="research")
        assert recovered.ok
        assert not recovered.recovered_tool_outcomes
        assert recovered.settled_tool_outcomes
        # Reopen knowledge and use a fresh ledger. Only settled receipts may restore bindings.
        store.close()
        store = KnowledgeStore(tmp_path / "knowledge")
        restored_tools = ResearchTools(Search(), store, KnowledgeChanges(store.root), session_id="s")
        monkeypatch.setattr(restored_tools.search, "search", lambda *a, **k: pytest.fail("recovery repeated search"))
        monkeypatch.setattr(restored_tools.search, "fetch", lambda *a, **k: pytest.fail("recovery repeated fetch"))
        monkeypatch.setattr(restored_tools, "save_finding", lambda *a, **k: pytest.fail("recovery repeated write"))
        monkeypatch.setattr("codey.operations.task_entry._entry_executors", lambda *a: (None, None, restored_tools))
        provider = TextConversation(json.dumps({"tool": "done", "args": answer_args("Helium supply depends on gas processing.")}))
        frame = workspace_frame("", provider, task="Research helium supply")
        frame.run_id = "r"
        frame.request = replace(frame.request, session_id="s", intent="research", max_turns=8)
        frame.task_kind = "research"
        frame.provider_id = "local"
        frame.recovered_tool_outcomes = recovered.recovered_tool_outcomes
        frame.settled_tool_outcomes = recovered.settled_tool_outcomes
        frame.recovered_tool_result_batch_id = recovered.recovered_tool_result_batch_id
        deps.state = SimpleNamespace(durable=deps.runtime_mutations.durable, set_provider_session=lambda *a: None)
        deps.knowledge_store = store
        outcome = run_entry_kernel(frame, RunWork([], ExecutionEvidence()),
            SimpleNamespace(on_event=lambda _: None, on_shell_request=None), deps, task_kind="research")
        assert outcome.event["stop_reason"] == "done", outcome.event
        for field in ("questions", "searches", "opened_sources", "evidence_items", "references", "read_passages", "investigations"):
            assert getattr(restored_tools.ledger, field) == getattr(ledger, field), field
        assert restored_tools.ledger.source_text_for_url(url) == ledger.source_text_for_url(url)
        assert calls == ["search", "open"]
    finally:
        store.close()
        sink.durable.close()



def test_failed_ledger_projection_does_not_commit_partial_task_facts():
    from codey.agents.request import RecoveredToolOutcome
    from codey.operations.kernel_errors import RecoveryFailed
    from codey.policies.task_policy import TaskPolicy
    from codey.toolchain.runtime import ToolOutcome
    from tests.support.session_projection import restore_task_session

    policy = TaskPolicy(frozenset({"control", "web.read"}))
    session = TaskSession(policy=policy)
    ledger = ResearchLedger()
    from codey.research.ledger_receipts import ledger_observation

    search_ledger = ResearchLedger()
    search_ledger.record_search("helium", [{"url": "https://example.com"}])
    rows = (
        RecoveredToolOutcome(ToolCall("web_search", {"query": "helium"}),
                             ToolOutcome("1. docs\n   https://example.com", True,
                                         canonical={"research_observation": ledger_observation(search_ledger, "web_search")}),
                             1, 0, effect_id="search"),
        RecoveredToolOutcome(ToolCall("open_url", {"url": "https://example.com"}),
                             ToolOutcome("source", True, canonical={"research_observation": {"bad": True}}),
                             2, 0, effect_id="open"),
    )
    frame = SimpleNamespace(run_id="r", recovered_tool_outcomes=(), settled_tool_outcomes=rows)
    with pytest.raises(RecoveryFailed, match="recovered facts replay"):
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
