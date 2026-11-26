"""A production-entry knowledge lifecycle, with deterministic provider replies."""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

import pytest

from codey.app.headless_runner import HeadlessRequest, run_headless
from codey.knowledge.concepts import build_unified_research_graph
from codey.knowledge.model import KnowledgeObject, KnowledgeReference
from codey.knowledge.store import KnowledgeStore
from codey.research.search_contracts import SearchOutcome
from codey.types.json_value import JsonObject, decode_json_object, encode_transport_value
from tests.support.search_fixture import search_outcome
from tests.test_research_submission_handoff_production import NativeExperimentProvider, install_http, requests
from tools.completion_explanation_ab import ExperimentProvider, Target


@pytest.mark.usefixtures("no_external_advisor_models", "scripted_local_api_connection", "public_source_dns")
@pytest.mark.parametrize("native", [False, True])
def test_production_entry_acquire_restart_associate_revise_and_trace(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, native: bool,
) -> None:
    root = tmp_path / "knowledge"
    url = "https://example.com/database-concurrency"
    quote = "Only one writer may commit at a time. Multiple readers can proceed."
    calls: list[tuple[str, str]] = []

    class Search:
        def search(self, query: str, limit: int = 8) -> SearchOutcome:
            calls.append(("search", query))
            return search_outcome(query, [{"title": "Concurrency experiment", "url": url, "snippet": quote}], limit=limit)

        def fetch(self, target: str) -> dict[str, object]:
            assert target == url
            calls.append(("fetch", target))
            return {"status": "ok", "url": url, "title": "Concurrency experiment", "text": quote,
                    "truncated": False}


    monkeypatch.setattr("codey.research.search_factory.default_research_search_provider", Search)
    monkeypatch.setattr("codey.operations.task_phases.dispatch.default_research_search_provider", Search)

    def run(stage: str, question: str, actions: list[tuple[str, JsonObject]]) -> Path:
        directory = tmp_path / stage
        directory.mkdir()
        replies: list[JsonObject] = [{"tool": tool, "args": args} for tool, args in actions]
        install_http(monkeypatch, iter(replies), native=native)
        provider_type = NativeExperimentProvider if native else ExperimentProvider
        provider = provider_type(Target("http://scripted.test/v1", "scripted-fixture", 123456789, 10),
                                      directory, fixed_acquisition=False)
        events: list[dict[str, object]] = []

        def connect(provider_id: str, *, port: int) -> ExperimentProvider:
            return provider

        result = run_headless(HeadlessRequest(project=None, task=question, intent="hybrid", provider_id="local",
            max_turns=len(actions) + 1, session_id=stage, state_home=tmp_path / "state",
            research_store_root=root, requested_capabilities=("web.read", "knowledge.read", "knowledge.write")),
            emit_jsonl=events.append, connect_provider=connect)
        assert result.exit_code == 0, (result, events[-3:])
        assert all(row.get("ok") for row in events if row.get("type") == "tool"), events
        return directory

    run("first", "Investigate database commit concurrency", [
        ("web_search", {"query": "database concurrent commits"}),
        ("open_url", {"url": url}),
        ("knowledge_write", {"type": "fact", "title": "Writer serialization", "body": quote,
            "aliases": ["unique gate", "嵌入式数据文件"],
            "conditions": {"mode": {"state": "reported", "value": "Measured database configuration"}},
            "evidence": [{"claim": "Writers serialize", "source_url": url, "excerpt": quote}]}),
        ("done", {"summary": "The measured configuration serializes writers; this does not establish queue behavior."}),
    ])
    store = KnowledgeStore(root)
    try:
        claim = next(hit.version for hit in store.search("unique gate", depth=0) if hit.version.value.kind == "claim")
        assert claim.value.conditions["mode"]["state"] == "reported"
    finally:
        store.close()

    candidate = KnowledgeObject("queue-analogy", "inference", "A queue with one commit gate may serialize commits.",
        "model_inference", aliases=("queue gate",), references=(KnowledgeReference(claim.value.id, 1, "depends_on"),))
    provider = run("second", "Does a queue's unique gate share the previously studied bottleneck?", [
        ("knowledge_search", {"query": "unique gate", "depth": 2}),
        ("knowledge_read", {"id": claim.value.id, "revision": 1}),
        ("knowledge_commit", {"commit_id": "analogy", "changes": [{"expected_revision": 0, "value": decode_json_object(encode_transport_value(candidate))}]}),
        ("done", {"summary": "Recalled via the gate alias and shared serialization constraint. Queue behavior remains a hypothesis.",
                  "knowledge_refs": [{"id": candidate.id, "revision": 1}, {"id": claim.value.id, "revision": 1}]}),
    ])
    first_prompt = json.dumps(requests(provider)[0]["messages"], ensure_ascii=False)
    assert claim.value.id in first_prompt
    assert "explicit name or alias" in first_prompt
    assert len(calls) == 2  # Recall does not acquire the original source again.

    revised = replace(claim.value, conditions={"mode": {"state": "reported", "value": "Single tested workload only"}})
    run("third", "Narrow the original measurement's applicability; keep the old queue premise pinned.", [
        ("knowledge_read", {"id": claim.value.id}),
        ("knowledge_commit", {"commit_id": "scope-revision", "changes": [{"expected_revision": 1, "value": decode_json_object(encode_transport_value(revised))}]}),
        ("knowledge_read", {"id": candidate.id}),
        ("done", {"summary": "The premise changed; reassess the candidate using the narrower measurement scope.",
                  "knowledge_refs": [{"id": candidate.id, "revision": 1}, {"id": claim.value.id, "revision": 2}]}),
    ])
    store = KnowledgeStore(root)
    try:
        current = store.repository.read(claim.value.id)
        prior = store.repository.read(claim.value.id, 1)
        dependent = store.repository.read(candidate.id)
        assert current is not None and current.revision == 2
        assert prior is not None and prior.value == claim.value
        assert dependent is not None and dependent.dependencies_changed
        # Reading a prior candidate does not adopt it into this session's graph.
        # Focus its branch explicitly to inspect the still-pinned old premise.
        graph = build_unified_research_graph(store, "third", focus_ids=(candidate.id,), depth=3)
        assert any(edge.kind == "depends_on" and edge.dst.endswith(claim.value.id + "@1") for edge in graph.edges)
        assert any(node.id.endswith(claim.value.id + "@2") for node in graph.nodes)
        assert any(node.kind == "evidence" for node in graph.nodes)
    finally:
        store.close()
