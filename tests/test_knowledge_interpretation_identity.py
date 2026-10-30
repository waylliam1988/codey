"""Located text and semantic support are deliberately separate facts."""

import json
from dataclasses import replace

import pytest

from codey.knowledge.archive import stable_identity
from codey.knowledge.changes import KnowledgeChanges
from codey.knowledge.model import KnowledgeMutation
from codey.knowledge.store import KnowledgeStore
from codey.policies.task_policy import TaskPolicy
from codey.research.tools import ResearchTools


def test_wrong_extraction_stays_unreviewed_after_save_reopen_recall_and_answer_adoption(tmp_path):
    store = KnowledgeStore(tmp_path)
    url = "https://example.org/wal"
    quote = "Writers and readers can run at the same time."
    wrong = "多个写入者可以同时提交。"
    try:
        tools = ResearchTools(None, store, KnowledgeChanges(tmp_path), session_id="first")
        tools.sources_read.add(url)
        tools.ledger.record_open(url, url, "WAL", quote)
        result = tools.knowledge_write({"type": "fact", "title": "WAL", "body": wrong,
            "sources": [url], "evidence": [{"claim": wrong, "source_url": url, "excerpt": quote}]})
        assert result.ok  # A locator check cannot discover this semantic error.
        note_id = tools.created_ids[0]
        claim_id = stable_identity("claim", note_id, wrong)
        version = store.repository.read(claim_id)
        assert version.value.origin == "model_extraction" and version.value.status == "candidate"
        assert version.interpretation_review == "unreviewed" and not version.dependencies_changed
        assert version.value.provenance["source_statement_type"] == "not_extracted"
        assert '"interpretation_review":"unreviewed"' in result.model_text
        # A model may write arbitrary commentary but cannot assert an assessment.
        forged = replace(version.value, provenance={**version.value.provenance, "reviewed": True})
        store.repository.commit("model-self-assertion", [KnowledgeMutation(forged, 1)])
        with pytest.raises(ValueError, match="unreviewed"):
            store.repository.commit("auto-promote", [KnowledgeMutation(replace(forged, status="active"), 2)])
        from codey.operations.research_iteration import _persist_synthesis

        _persist_synthesis(tools, "WAL", wrong + " [1]", session_id="first", project="",
                           run_id="adopted", on_event=lambda _: None,
                           policy=TaskPolicy(frozenset({"control", "knowledge.write"})))
    finally:
        store.close()
    store = KnowledgeStore(tmp_path)
    try:
        tools = ResearchTools(None, store, None, session_id="new-session")
        payload = json.loads(tools.knowledge_read(claim_id).model_text)
        assert payload["interpretation_review"] == "unreviewed"
        assert not payload["dependencies_changed"] and "needs_review" not in payload
        assert payload["status"] == "candidate" and payload["origin"] == "model_extraction"
        recalled = tools.knowledge_search("WAL").model_text
        assert '"interpretation_review":"unreviewed"' in recalled
        assert "not that a claim is true" in recalled
        answer = store.repository.read(stable_identity("answer", "adopted", "initial"))
        assert answer.interpretation_review == "unreviewed"
        from codey.knowledge.version_graph import build_version_graph

        graph = build_version_graph(store, "first", focus_ids=[claim_id])
        assert any(node.interpretation_review == "unreviewed" and not node.dependencies_changed
                   for node in graph.nodes if node.note_type == "claim")
    finally:
        store.close()
