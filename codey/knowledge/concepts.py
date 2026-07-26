"""Concept Graph read model.

Concepts are virtual nodes over the SQLite cache: declared relations from note
frontmatter become edges, note tags weight the vocabulary, and recent synthesis
notes anchor reports to concepts. Missing-link suggestions are text on concept
nodes only — never persisted, never rendered as factual edges. The Evidence
Graph in graph.py stays untouched.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass

from codey.knowledge.concept_schema import normalize_concept
from codey.knowledge.graph import GraphEdge, GraphNode, ResearchGraphArtifact
from codey.knowledge.store import KnowledgeStore

DEFAULT_CONCEPT_NODE_LIMIT = 64
DEFAULT_CONCEPT_EDGE_LIMIT = 128
MAX_SYNTHESIS_NODES = 12
MAX_MISSING_SUGGESTIONS = 6
MAX_EXCERPT_RELATIONS = 6
_EDGE_ROW_SCAN = 2048
_TAG_ROW_SCAN = 4096


@dataclass(frozen=True)
class SupportRef:
    note_id: str
    title: str
    session_id: str = ""


def concept_node_id(concept: str) -> str:
    return f"concept:{concept}"


def _bounded_int(value: object, default: int, minimum: int, maximum: int) -> int:
    try:
        parsed = int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        parsed = default
    return max(minimum, min(parsed, maximum))


class ConceptGraphBuilder:
    def __init__(self, store: KnowledgeStore) -> None:
        self.store = store

    def build_for_session(
        self,
        session_id: str = "",
        *,
        node_limit: int = DEFAULT_CONCEPT_NODE_LIMIT,
        edge_limit: int = DEFAULT_CONCEPT_EDGE_LIMIT,
    ) -> ResearchGraphArtifact:
        session_id = (session_id or "").strip()
        node_limit = _bounded_int(node_limit, DEFAULT_CONCEPT_NODE_LIMIT, 8, 200)
        edge_limit = _bounded_int(edge_limit, DEFAULT_CONCEPT_EDGE_LIMIT, 8, 400)
        edge_rows = self.store.index.concept_edge_rows(
            _EDGE_ROW_SCAN, session_id=session_id
        )
        tag_rows = self.store.index.tag_concept_rows(
            _TAG_ROW_SCAN, session_id=session_id
        )

        declared = self._aggregate_declared(edge_rows)
        tag_notes, session_concepts, syntheses = self._aggregate_tags(tag_rows, session_id)
        weights = self._concept_weights(declared, tag_notes)
        if not weights:
            return ResearchGraphArtifact(
                depth=1,
                warnings=(
                    "No concepts yet. Save notes with tags or relations to grow the Concept Graph.",
                ),
            )

        kept = self._select_concepts(
            declared, weights, session_concepts, session_id, node_limit
        )
        suggestions = _missing_suggestions(declared, kept)
        relation_sections = _relation_sections(
            declared, set(kept), weights, session_concepts, session_id
        )
        nodes = [
            GraphNode(
                id=concept_node_id(concept),
                label=concept,
                kind="concept",
                note_type="concept",
                weight=weights[concept],
                focus=concept in session_concepts,
                excerpt=_concept_excerpt(
                    relation_sections.get(concept, {}), suggestions.get(concept, [])
                ),
                virtual=True,
            )
            for concept in kept
        ]
        edges = self._declared_edges(
            declared, set(kept), weights, session_concepts, session_id, edge_limit
        )
        nodes, edges = self._append_syntheses(
            nodes,
            edges,
            syntheses,
            set(kept),
            session_id,
            node_budget=node_limit - len(nodes),
            edge_budget=edge_limit - len(edges),
        )

        focus_ids = tuple(node.id for node in nodes if node.focus)
        center_id = focus_ids[0] if focus_ids else nodes[0].id
        return ResearchGraphArtifact(
            center_id=center_id,
            focus_ids=focus_ids,
            depth=1,
            nodes=tuple(nodes),
            edges=tuple(edges),
        )

    def _aggregate_declared(
        self,
        edge_rows: list[dict],
    ) -> dict[tuple[str, str, str], list[SupportRef]]:
        declared: dict[tuple[str, str, str], list[SupportRef]] = {}
        for row in edge_rows:
            key = (str(row["src"]), str(row["dst"]), str(row["kind"]))
            notes = declared.setdefault(key, [])
            note_id = str(row["note_id"])
            title = str(row.get("title") or note_id)
            session_id = str(row.get("session_id") or "")
            if note_id not in {existing.note_id for existing in notes}:
                notes.append(SupportRef(note_id, title, session_id))
        return declared

    def _aggregate_tags(
        self, tag_rows: list[dict], session_id: str
    ) -> tuple[dict[str, set[str]], set[str], list[dict]]:
        tag_notes: dict[str, set[str]] = {}
        session_concepts: set[str] = set()
        syntheses: list[dict] = []
        synthesis_by_id: dict[str, dict] = {}
        for row in tag_rows:
            concept = normalize_concept(row.get("tag"))
            if not concept:
                continue
            note_id = str(row["note_id"])
            tag_notes.setdefault(concept, set()).add(note_id)
            if session_id and str(row.get("session_id") or "") == session_id:
                session_concepts.add(concept)
            if str(row.get("type") or "") == "synthesis":
                entry = synthesis_by_id.get(note_id)
                if entry is None:
                    entry = {
                        "id": note_id,
                        "title": str(row.get("title") or note_id),
                        "session_id": str(row.get("session_id") or ""),
                        "concepts": [],
                    }
                    synthesis_by_id[note_id] = entry
                    syntheses.append(entry)
                entry["concepts"].append(concept)
        return tag_notes, session_concepts, syntheses

    def _concept_weights(
        self,
        declared: dict[tuple[str, str, str], list[SupportRef]],
        tag_notes: dict[str, set[str]],
    ) -> dict[str, float]:
        degree: Counter[str] = Counter()
        for src, dst, _kind in declared:
            degree[src] += 1
            degree[dst] += 1
        weights: dict[str, float] = {}
        for concept in set(degree) | set(tag_notes):
            mentions = len(tag_notes.get(concept, ()))
            if concept in degree:
                # Declared endpoints stay above the UI label threshold (2.6).
                weights[concept] = min(6.0, 2.6 + 0.4 * (degree[concept] - 1) + 0.15 * mentions)
            else:
                weights[concept] = min(2.4, 1.2 + 0.3 * mentions)
        return weights

    def _select_concepts(
        self,
        declared: dict[tuple[str, str, str], list[SupportRef]],
        weights: dict[str, float],
        session_concepts: set[str],
        session_id: str,
        node_limit: int,
    ) -> list[str]:
        declared_concepts = {c for src, dst, _kind in declared for c in (src, dst)}
        kept: list[str] = []
        kept_set: set[str] = set()

        for (src, dst, _kind), _notes in sorted(
            declared.items(),
            key=lambda item: _relation_rank(item, weights, session_concepts, session_id),
        ):
            missing = [concept for concept in (src, dst) if concept not in kept_set]
            if len(kept) + len(missing) > node_limit:
                continue
            for concept in missing:
                kept.append(concept)
                kept_set.add(concept)
            if len(kept) >= node_limit:
                break

        remaining = sorted(
            (concept for concept in weights if concept not in kept_set),
            key=lambda c: (
                c not in session_concepts,
                c not in declared_concepts,
                -weights[c],
                c,
            ),
        )
        for concept in remaining:
            if len(kept) >= node_limit:
                break
            kept.append(concept)
            kept_set.add(concept)
        return kept

    def _declared_edges(
        self,
        declared: dict[tuple[str, str, str], list[SupportRef]],
        kept: set[str],
        weights: dict[str, float],
        session_concepts: set[str],
        session_id: str,
        edge_limit: int,
    ) -> list[GraphEdge]:
        ranked = sorted(
            declared.items(),
            key=lambda item: _relation_rank(item, weights, session_concepts, session_id),
        )
        edges: list[GraphEdge] = []
        for (src, dst, kind), notes in ranked:
            if len(edges) >= edge_limit:
                break
            if src not in kept or dst not in kept:
                continue
            edges.append(
                GraphEdge(
                    src=concept_node_id(src),
                    dst=concept_node_id(dst),
                    kind=kind,
                    label=f"{kind} ({len(notes)})" if len(notes) > 1 else kind,
                    weight=min(3.0, float(len(notes))),
                )
            )
        return edges

    def _append_syntheses(
        self,
        nodes: list[GraphNode],
        edges: list[GraphEdge],
        syntheses: list[dict],
        kept: set[str],
        session_id: str,
        *,
        node_budget: int,
        edge_budget: int,
    ) -> tuple[list[GraphNode], list[GraphEdge]]:
        # Syntheses only spend what the node/edge limits left over, so
        # small-limit API requests stay small.
        added = 0
        max_nodes = min(MAX_SYNTHESIS_NODES, max(0, node_budget))
        for synthesis in syntheses:
            if added >= max_nodes or edge_budget <= 0:
                break
            concepts = [c for c in dict.fromkeys(synthesis["concepts"]) if c in kept]
            if not concepts:
                continue
            added += 1
            nodes.append(
                GraphNode(
                    id=synthesis["id"],
                    label=synthesis["title"],
                    kind="synthesis",
                    note_type="synthesis",
                    weight=2.2,
                    focus=bool(session_id and synthesis["session_id"] == session_id),
                )
            )
            for concept in concepts:
                if edge_budget <= 0:
                    break
                edge_budget -= 1
                edges.append(
                    GraphEdge(
                        src=synthesis["id"],
                        dst=concept_node_id(concept),
                        kind="tagged",
                        weight=1.0,
                    )
                )
        return nodes, edges


def _missing_suggestions(
    declared: dict[tuple[str, str, str], list[SupportRef]],
    kept: list[str],
) -> dict[str, list[str]]:
    """Unproven link candidates from declared adjacency only (never co-tags).

    Pure graph query: two concepts that share a declared neighbor but have no
    declared edge between them. Capped, text-only, phrased as questions.
    """
    adjacency: dict[str, set[str]] = {}
    linked: set[frozenset[str]] = set()
    for src, dst, _kind in declared:
        adjacency.setdefault(src, set()).add(dst)
        adjacency.setdefault(dst, set()).add(src)
        linked.add(frozenset((src, dst)))
    kept_declared = [c for c in kept if c in adjacency]
    candidates: list[tuple[int, str, str, set[str]]] = []
    for i, a in enumerate(kept_declared):
        for b in kept_declared[i + 1 :]:
            if frozenset((a, b)) in linked:
                continue
            shared = adjacency[a] & adjacency[b]
            if shared:
                candidates.append((len(shared), a, b, shared))
    candidates.sort(key=lambda item: (-item[0], item[1], item[2]))
    suggestions: dict[str, list[str]] = {}
    for _score, a, b, shared in candidates[:MAX_MISSING_SUGGESTIONS]:
        via = ", ".join(sorted(shared)[:3])
        line = f"- {a} ? {b} (shared neighbor: {via})"
        suggestions.setdefault(a, []).append(line)
        suggestions.setdefault(b, []).append(line)
    return suggestions


def _relation_rank(
    item: tuple[tuple[str, str, str], list[SupportRef]],
    weights: dict[str, float],
    session_concepts: set[str],
    session_id: str,
) -> tuple[object, ...]:
    (src, dst, kind), notes = item
    has_current_support = bool(
        session_id and any(note.session_id == session_id for note in notes)
    )
    has_session_endpoint = src in session_concepts or dst in session_concepts
    kind_bonus = {
        "affects": 0,
        "causes": 0,
        "uses": 1,
        "part_of": 2,
        "enables": 2,
        "relates": 3,
    }.get(kind, 4)
    return (
        -int(has_current_support),
        -int(has_session_endpoint),
        -len(notes),
        kind_bonus,
        -(weights.get(src, 0.0) + weights.get(dst, 0.0)),
        src,
        dst,
        kind,
    )


def _relation_sections(
    declared: dict[tuple[str, str, str], list[SupportRef]],
    kept: set[str],
    weights: dict[str, float],
    session_concepts: set[str],
    session_id: str,
) -> dict[str, dict[str, list[str]]]:
    """Per-concept declared relations grouped by edge direction.

    The canvas draws undirected lines, so the node detail is where users read
    which way a relation points and which notes support it.
    """
    sections: dict[str, dict[str, list[str]]] = {}
    ranked = sorted(
        declared.items(),
        key=lambda item: _relation_rank(item, weights, session_concepts, session_id),
    )
    for (src, dst, kind), notes in ranked:
        support = _support_line(notes)
        if src in kept:
            outgoing = sections.setdefault(src, {}).setdefault("outgoing", [])
            if len(outgoing) < MAX_EXCERPT_RELATIONS:
                outgoing.append(f"- {src} --{kind}--> {dst}\n  {support}")
        if dst in kept:
            incoming = sections.setdefault(dst, {}).setdefault("incoming", [])
            if len(incoming) < MAX_EXCERPT_RELATIONS:
                incoming.append(f"- {src} --{kind}--> {dst}\n  {support}")
    return sections


def _support_line(notes: list[SupportRef]) -> str:
    titles = [note.title for note in notes[:3]]
    suffix = "; ..." if len(notes) > 3 else ""
    noun = "note" if len(notes) == 1 else "notes"
    return f"{len(notes)} supporting {noun}: {'; '.join(titles)}{suffix}"


def _concept_excerpt(relation_sections: dict[str, list[str]], suggestion_lines: list[str]) -> str:
    parts: list[str] = []
    relation_parts: list[str] = []
    outgoing = relation_sections.get("outgoing", [])
    incoming = relation_sections.get("incoming", [])
    if outgoing:
        relation_parts.append("## Outgoing\n" + "\n".join(outgoing))
    if incoming:
        relation_parts.append("## Incoming\n" + "\n".join(incoming))
    if relation_parts:
        parts.append("## Declared Relations\n" + "\n\n".join(relation_parts))
    if suggestion_lines:
        parts.append(
            "## Open Questions\n" + "\n".join(suggestion_lines) + "\n\nUnproven; not facts."
        )
    return "\n\n".join(parts)
