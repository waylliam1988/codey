from dataclasses import replace

from codey.knowledge.concepts import build_unified_research_graph
from codey.knowledge.model import KnowledgeMutation as M
from codey.knowledge.note import KnowledgeNote
from codey.knowledge.store import KnowledgeStore
from codey.knowledge.version_graph import version_node_id
from tests.test_knowledge_repository import example


def test_graph_uses_exact_knowledge_versions_and_explains_edges(tmp_path):
    store = KnowledgeStore(tmp_path)
    try:
        note = KnowledgeNote(id="report-note", title="Research", body="Report", session_id="research-1")
        store.publish_note(note, "research-1", example())
        store.repository.commit("revision", [M(replace(store.repository.read("claim").value, status="withdrawn"), 1)])
        graph = build_unified_research_graph(store, "research-1", depth=3)
        ids = {node.id for node in graph.nodes}
        assert version_node_id("claim", 1) in ids
        claim = next(node for node in graph.nodes if node.id == version_node_id("claim", 1))
        assert claim.status == "needs_review"
        assert "WAL (reported)" in claim.excerpt
        edge = next(edge for edge in graph.edges if edge.kind == "supported_by")
        assert edge.src == version_node_id("claim", 1)
        assert edge.dst == version_node_id("excerpt", 1)
        assert "https://example.org/wal" in "\n".join(node.excerpt for node in graph.nodes)
        assert edge.to_dict()["detail"].startswith("Relation: supported_by")
        assert all(edge.src in ids and edge.dst in ids for edge in graph.edges)
    finally:
        store.close()


def test_graph_limits_and_scope_filtering_do_not_leak_private_versions(tmp_path):
    store = KnowledgeStore(tmp_path)
    try:
        store.repository.commit("shared", example())
        note = KnowledgeNote(id="scoped", body="Private", project="project-a", session_id="a")
        store.publish_note(note, "scoped", [M(replace(example()[0].value, id="private", scope="project", scope_id="project-a"))])
        denied = build_unified_research_graph(store, "other-session", focus_ids=["private"], depth=3, node_limit=8, edge_limit=8)
        assert not any("private" in node.id for node in denied.nodes)
        graph = build_unified_research_graph(store, "a", focus_ids=["sqlite"], depth=3, node_limit=8, edge_limit=8)
        assert len(graph.nodes) <= 8 and len(graph.edges) <= 8
    finally:
        store.close()
