"""A production-entry knowledge lifecycle, with deterministic provider replies."""

import json
from dataclasses import replace
from unittest.mock import patch

import pytest

from codey.app.headless_runner import HeadlessRequest, run_headless
from codey.knowledge.concepts import build_unified_research_graph
from codey.knowledge.model import KnowledgeObject, KnowledgeReference
from codey.knowledge.store import KnowledgeStore
from tests.test_headless_runner import _RouteProvider


@pytest.mark.usefixtures("no_external_advisor_models")
def test_production_entry_acquire_restart_associate_revise_and_trace(tmp_path):
    root = tmp_path / "knowledge"
    url = "https://example.org/database-concurrency"
    quote = "Only one writer may commit at a time. Multiple readers can proceed."
    calls = []

    class Search:
        def search(self, query, limit=8):
            calls.append(("search", query))
            return [{"title": "Concurrency experiment", "url": url, "snippet": quote}]

        def fetch(self, target):
            assert target == url
            calls.append(("fetch", target))
            return {"status": "ok", "url": url, "title": "Concurrency experiment", "text": quote,
                    "truncated": False}

        def close(self):
            pass

    def run(stage, question, actions):
        provider = _RouteProvider([json.dumps({"tool": tool, "args": args}) for tool, args in actions])
        events = []
        with patch("codey.research.search_factory.default_research_search_provider", Search):
            result = run_headless(HeadlessRequest(project=None, task=question, intent="hybrid", provider_id="qwen",
                max_turns=len(actions) + 1, session_id=stage, state_home=tmp_path / "state",
                research_store_root=root, requested_capabilities=("web.read", "knowledge.read", "knowledge.write")),
                emit_jsonl=events.append, connect_provider=lambda *_a, **_kw: provider)
        assert result.exit_code == 0, (result, events[-3:], provider.prompts[-1])
        assert all(row.get("ok") for row in events if row.get("type") == "tool")
        return provider

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
        ("knowledge_commit", {"commit_id": "analogy", "changes": [{"expected_revision": 0, "value": candidate.to_dict()}]}),
        ("done", {"summary": "Recalled via the gate alias and shared serialization constraint. Queue behavior remains a hypothesis.",
                  "knowledge_refs": [{"id": candidate.id, "revision": 1}, {"id": claim.value.id, "revision": 1}]}),
    ])
    assert claim.value.id in provider.prompts[0]
    assert "explicit name or alias" in provider.prompts[0]
    assert len(calls) == 2  # Recall does not acquire the original source again.

    revised = replace(claim.value, conditions={"mode": {"state": "reported", "value": "Single tested workload only"}})
    run("third", "Narrow the original measurement's applicability; keep the old queue premise pinned.", [
        ("knowledge_read", {"id": claim.value.id}),
        ("knowledge_commit", {"commit_id": "scope-revision", "changes": [{"expected_revision": 1, "value": revised.to_dict()}]}),
        ("knowledge_read", {"id": candidate.id}),
        ("done", {"summary": "The premise changed; reassess the candidate using the narrower measurement scope.",
                  "knowledge_refs": [{"id": candidate.id, "revision": 1}, {"id": claim.value.id, "revision": 2}]}),
    ])
    store = KnowledgeStore(root)
    try:
        assert store.repository.read(claim.value.id).revision == 2
        assert store.repository.read(claim.value.id, 1).value == claim.value
        assert store.repository.read(candidate.id).dependencies_changed
        graph = build_unified_research_graph(store, "third", depth=3)
        assert any(edge.kind == "depends_on" and edge.dst.endswith(claim.value.id + "@1") for edge in graph.edges)
        assert any(node.id.endswith(claim.value.id + "@2") for node in graph.nodes)
        assert any(node.kind == "evidence" for node in graph.nodes)
    finally:
        store.close()
