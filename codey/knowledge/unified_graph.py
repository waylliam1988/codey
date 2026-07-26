"""Unified Research graph read model.

This is a presentation composer over the existing concept and evidence graph
read models. It does not persist new graph state and does not merge their
semantics: declared concept relations remain concept edges, evidence links
remain note/source edges, and tag edges only connect visible notes to concepts.
"""

from __future__ import annotations

from codey.knowledge.concept_schema import normalize_concept
from codey.knowledge.concepts import ConceptGraphBuilder, concept_node_id
from codey.knowledge.graph import (
    DEFAULT_EDGE_LIMIT,
    DEFAULT_NODE_LIMIT,
    GraphEdge,
    GraphNode,
    KnowledgeGraphBuilder,
    ResearchGraphArtifact,
)
from codey.knowledge.store import KnowledgeStore

MAX_UNIFIED_DEPTH = 3

_KIND_PRIORITY = {
    "concept": 100,
    "synthesis": 80,
    "decision": 75,
    "fact": 70,
    "conclusion": 65,
    "source": 60,
    "hypothesis": 55,
    "question": 50,
    "counterpoint": 45,
    "source_url": 20,
}


class UnifiedResearchGraphBuilder:
    def __init__(self, store: KnowledgeStore | None) -> None:
        self.store = store

    def build_for_session(
        self,
        session_id: str = "",
        *,
        focus_ids: tuple[str, ...] | list[str] = (),
        depth: int = 1,
        node_limit: int = DEFAULT_NODE_LIMIT,
        edge_limit: int = DEFAULT_EDGE_LIMIT,
        counterpoints: tuple[str, ...] | list[str] = (),
    ) -> ResearchGraphArtifact:
        if self.store is None:
            return ResearchGraphArtifact(warnings=("Research is not configured",))
        depth = _bounded_int(depth, 1, 1, MAX_UNIFIED_DEPTH)
        node_limit = _bounded_int(node_limit, DEFAULT_NODE_LIMIT, 8, 200)
        edge_limit = _bounded_int(edge_limit, DEFAULT_EDGE_LIMIT, 8, 400)

        evidence_depth = 0 if depth == 1 else 1
        include_sources = depth >= 3
        evidence = KnowledgeGraphBuilder(self.store).build_for_session(
            session_id,
            focus_ids=focus_ids,
            depth=evidence_depth,
            include_sources=include_sources,
            node_limit=node_limit,
            edge_limit=edge_limit,
            counterpoints=counterpoints,
        )
        concept = ConceptGraphBuilder(self.store).build_for_session(
            session_id,
            node_limit=node_limit,
            edge_limit=edge_limit,
        )

        nodes_by_id: dict[str, GraphNode] = {}
        edges_by_id: dict[tuple[str, str, str], GraphEdge] = {}
        protected_node_ids: set[str] = set()
        protected_edge_keys: set[tuple[str, str, str]] = set()
        warnings = [*concept.warnings, *evidence.warnings]

        for node in concept.nodes:
            if node.kind == "concept":
                nodes_by_id[node.id] = node
        for edge in concept.edges:
            if edge.src in nodes_by_id and edge.dst in nodes_by_id:
                edges_by_id[(edge.src, edge.dst, edge.kind)] = edge

        evidence_note_ids: list[str] = []
        for node in evidence.nodes:
            nodes_by_id[node.id] = node
            protected_node_ids.add(node.id)
            if not node.virtual and node.kind != "source_url":
                evidence_note_ids.append(node.id)
        for edge in evidence.edges:
            key = (edge.src, edge.dst, edge.kind)
            edges_by_id[key] = edge
            protected_edge_keys.add(key)

        self._append_visible_note_tags(nodes_by_id, edges_by_id, evidence_note_ids)

        nodes = _trim_nodes(
            list(nodes_by_id.values()),
            node_limit,
            protected_ids=protected_node_ids,
        )
        kept = {node.id for node in nodes}
        edges = [
            edge for edge in edges_by_id.values()
            if edge.src in kept and edge.dst in kept
        ]
        edges = _trim_edges(edges, edge_limit, protected_keys=protected_edge_keys)
        focus_ids_out = tuple(node.id for node in nodes if node.focus)
        center_id = _center_id(nodes, evidence.center_id, focus_ids_out)
        return ResearchGraphArtifact(
            center_id=center_id,
            focus_ids=focus_ids_out,
            depth=depth,
            nodes=tuple(nodes),
            edges=tuple(edges),
            warnings=tuple(dict.fromkeys(warnings)),
        )

    def _append_visible_note_tags(
        self,
        nodes_by_id: dict[str, GraphNode],
        edges_by_id: dict[tuple[str, str, str], GraphEdge],
        note_ids: list[str],
    ) -> None:
        for row in self.store.index.tags_for(note_ids, active_only=True):
            note_id = str(row.get("note_id") or "")
            if note_id not in nodes_by_id:
                continue
            concept = normalize_concept(row.get("tag"))
            if not concept:
                continue
            concept_id = concept_node_id(concept)
            if concept_id not in nodes_by_id:
                nodes_by_id[concept_id] = GraphNode(
                    id=concept_id,
                    label=concept,
                    kind="concept",
                    note_type="concept",
                    weight=2.8 if nodes_by_id[note_id].focus else 2.2,
                    focus=nodes_by_id[note_id].focus,
                    virtual=True,
                )
            key = (note_id, concept_id, "tagged")
            edges_by_id.setdefault(
                key,
                GraphEdge(
                    src=note_id,
                    dst=concept_id,
                    kind="tagged",
                    label="tagged",
                    weight=1.0,
                    virtual=True,
                ),
            )


def _bounded_int(value: object, default: int, minimum: int, maximum: int) -> int:
    try:
        parsed = int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        parsed = default
    return max(minimum, min(parsed, maximum))


def _trim_nodes(
    nodes: list[GraphNode],
    limit: int,
    *,
    protected_ids: set[str] | None = None,
) -> list[GraphNode]:
    if len(nodes) <= limit:
        return nodes
    protected_ids = protected_ids or set()
    protected = [node for node in nodes if node.id in protected_ids]
    if len(protected) >= limit:
        return sorted(protected, key=_node_rank, reverse=True)[:limit]
    protected_seen = {node.id for node in protected}
    remaining = [
        node for node in sorted(nodes, key=_node_rank, reverse=True)
        if node.id not in protected_seen
    ]
    return protected + remaining[: limit - len(protected)]


def _node_rank(node: GraphNode) -> tuple[object, ...]:
    return (
        1 if node.focus else 0,
        _KIND_PRIORITY.get(node.kind, 10),
        float(node.weight or 0),
        node.label,
        node.id,
    )


def _trim_edges(
    edges: list[GraphEdge],
    limit: int,
    *,
    protected_keys: set[tuple[str, str, str]] | None = None,
) -> list[GraphEdge]:
    if len(edges) <= limit:
        return edges
    protected_keys = protected_keys or set()
    protected = [edge for edge in edges if (edge.src, edge.dst, edge.kind) in protected_keys]
    if len(protected) >= limit:
        return sorted(protected, key=_edge_rank, reverse=True)[:limit]
    protected_seen = {(edge.src, edge.dst, edge.kind) for edge in protected}
    remaining = [
        edge for edge in sorted(edges, key=_edge_rank, reverse=True)
        if (edge.src, edge.dst, edge.kind) not in protected_seen
    ]
    return protected + remaining[: limit - len(protected)]


def _edge_rank(edge: GraphEdge) -> tuple[object, ...]:
    kind_bonus = {
        "affects": 80,
        "causes": 80,
        "uses": 75,
        "part_of": 70,
        "enables": 70,
        "derives": 60,
        "supports": 55,
        "tagged": 35,
        "cites": 20,
    }.get(edge.kind, 30)
    return (kind_bonus, float(edge.weight or 0), edge.src, edge.dst)


def _center_id(
    nodes: list[GraphNode],
    evidence_center_id: str,
    focus_ids: tuple[str, ...],
) -> str:
    focus_concepts = [node.id for node in nodes if node.focus and node.kind == "concept"]
    if focus_concepts:
        return focus_concepts[0]
    if evidence_center_id and any(node.id == evidence_center_id for node in nodes):
        return evidence_center_id
    if focus_ids:
        return focus_ids[0]
    return nodes[0].id if nodes else ""
