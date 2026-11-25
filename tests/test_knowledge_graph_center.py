"""The research graph always includes its center within the node budget."""

from contextlib import closing
from pathlib import Path

from codey.knowledge.graph import KnowledgeGraphBuilder
from codey.knowledge.note import KnowledgeNote
from codey.knowledge.store import KnowledgeStore


def test_graph_retains_first_available_focus_under_node_limit(tmp_path: Path) -> None:
    with closing(KnowledgeStore(tmp_path)) as store:
        center = KnowledgeNote("question", type="question", body="What needs to be explained?")
        facts = [KnowledgeNote(f"fact-{i}", type="fact", body=f"Supporting fact {i}") for i in range(8)]
        for note in [center, *facts]:
            store.write_note(note)
        graph = KnowledgeGraphBuilder(store).build_for_session(
            focus_ids=["missing", center.id, *(fact.id for fact in facts)],
            depth=0,
            node_limit=8,
        )

    assert len(graph.nodes) == 8
    assert graph.center_id == center.id
    assert graph.focus_ids[0] == graph.center_id
    assert graph.center_id in {node.id for node in graph.nodes if node.focus}
