from __future__ import annotations

from types import SimpleNamespace

from codey.operations import task_entry
from codey.operations.result import ModeOutcome


def test_hybrid_completion_event_contains_research_payload(monkeypatch) -> None:
    class Ledger:
        searches = [SimpleNamespace(query="graph" )]

        def search_results_payload(self):
            return [{"url": "https://example.test"}]

        def opened_sources_payload(self):
            return []

        def evidence_payload(self):
            return [{"evidence_id": "e1"}]

        def coverage_payload(self):
            return {"matched": 1, "total": 1}

    tools = SimpleNamespace(
        ledger=Ledger(), created_ids=["note-graph"], updated_ids=[],
        sources_read={"https://example.test"}, links_created=0,
    )
    outcome = ModeOutcome({
        "type": "task_done", "run_id": "r", "session_id": "s", "summary": "done",
        "stop_reason": "done", "turns": 2, "max_turns": 8, "provider": "local",
        "mode": "hybrid", "receipt": {"display": {"summary": "done"}},
    })
    monkeypatch.setattr(task_entry, "run_entry_kernel", lambda *args, **kwargs: outcome)
    frame = SimpleNamespace(
        task_kind="hybrid", request=SimpleNamespace(task="graph"),
        entry_session=SimpleNamespace(), entry_research_tools=tools,
    )
    result = task_entry.run_task_mode(frame, SimpleNamespace(evidence=None), SimpleNamespace(), object())
    assert result.event["research"]["notes_created"] == ["note-graph"]
    assert result.event["research"]["queries"] == ["graph"]


def test_hybrid_completion_records_only_real_knowledge_changes(monkeypatch) -> None:
    class Ledger:
        searches = []

        def search_results_payload(self):
            return []

        def opened_sources_payload(self):
            return []

        def evidence_payload(self):
            return []

        def coverage_payload(self):
            return {}

    class Changes:
        def has_changes(self):
            return True

    tools = SimpleNamespace(
        ledger=Ledger(), created_ids=[], updated_ids=[], sources_read=set(), links_created=0,
        changes=Changes(), synthesis_id="",
    )
    outcome = ModeOutcome({"type": "task_done", "summary": "done", "receipt": {}})
    monkeypatch.setattr(task_entry, "run_entry_kernel", lambda *args, **kwargs: outcome)
    recorded = []
    frame = SimpleNamespace(
        run_id="run-1", task_kind="hybrid", request=SimpleNamespace(task="q", session_id="s"),
        entry_session=SimpleNamespace(), entry_research_tools=tools,
    )
    deps = SimpleNamespace(state=SimpleNamespace(record_research_changes=lambda *args: recorded.append(args)))
    task_entry.run_task_mode(frame, SimpleNamespace(evidence=None), SimpleNamespace(), deps)
    assert recorded and recorded[0][0] == "run-1" and recorded[0][2] == "s"


def test_research_payload_exposes_registered_restore_availability() -> None:
    from codey.operations.research_flow import research_payload
    result = SimpleNamespace(
        synthesis_id="synthesis-1", notes_created=[], notes_updated=[], sources_read=0,
        source_urls=[], queries=[], search_results=[], opened_sources=[], coverage={},
        citation_map=[], evidence_items=[], counterpoints=[], quality_warnings=[], max_turns_used=1,
    )
    assert research_payload(result, restoreable=True)["restoreable"] is True
    assert research_payload(result, restoreable=False)["restoreable"] is False
