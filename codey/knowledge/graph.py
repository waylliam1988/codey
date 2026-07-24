"""Research graph read model.

The Markdown vault remains authoritative. This module only turns the current
SQLite index into a bounded graph artifact for the Research drawer.
"""

from __future__ import annotations

import hashlib
import re
from collections import Counter
from dataclasses import dataclass
from urllib.parse import urlparse

from codey.knowledge.store import KnowledgeStore

GRAPH_EDGE_KINDS = (
    "relates",
    "supports",
    "contradicts",
    "derives",
    "implements",
    "verifies",
    "cites",
)
DEFAULT_NODE_LIMIT = 96
DEFAULT_EDGE_LIMIT = 192
MAX_DEPTH = 2

_EDGE_PRIORITY = {
    "contradicts": 90,
    "implements": 80,
    "verifies": 75,
    "derives": 60,
    "supports": 55,
    "cites": 35,
    "relates": 20,
}
_TYPE_PRIORITY = {
    "synthesis": 100,
    "decision": 80,
    "implementation": 75,
    "verification": 70,
    "fact": 65,
    "conclusion": 60,
    "source": 55,
    "hypothesis": 45,
    "question": 40,
    "project_note": 35,
}


@dataclass(frozen=True)
class GraphNode:
    id: str
    label: str
    kind: str
    note_type: str = ""
    status: str = ""
    path: str = ""
    url: str = ""
    weight: float = 1.0
    focus: bool = False
    excerpt: str = ""
    virtual: bool = False

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "label": self.label,
            "kind": self.kind,
            "note_type": self.note_type,
            "status": self.status,
            "path": self.path,
            "url": self.url,
            "weight": self.weight,
            "focus": self.focus,
            "excerpt": self.excerpt,
            "virtual": self.virtual,
        }


@dataclass(frozen=True)
class GraphEdge:
    src: str
    dst: str
    kind: str
    label: str = ""
    weight: float = 1.0
    virtual: bool = False

    def to_dict(self) -> dict:
        return {
            "id": f"{self.src}->{self.dst}:{self.kind}",
            "src": self.src,
            "dst": self.dst,
            "kind": self.kind,
            "label": self.label or self.kind,
            "weight": self.weight,
            "virtual": self.virtual,
        }


@dataclass(frozen=True)
class ResearchGraphArtifact:
    center_id: str = ""
    focus_ids: tuple[str, ...] = ()
    depth: int = 1
    nodes: tuple[GraphNode, ...] = ()
    edges: tuple[GraphEdge, ...] = ()
    warnings: tuple[str, ...] = ()

    def to_dict(self) -> dict:
        return {
            "center_id": self.center_id,
            "focus_ids": list(self.focus_ids),
            "depth": self.depth,
            "count": {
                "nodes": len(self.nodes),
                "edges": len(self.edges),
            },
            "nodes": [node.to_dict() for node in self.nodes],
            "edges": [edge.to_dict() for edge in self.edges],
            "warnings": list(self.warnings),
        }


class KnowledgeGraphBuilder:
    def __init__(self, store: KnowledgeStore | None) -> None:
        self.store = store

    def build_for_session(
        self,
        session_id: str = "",
        *,
        focus_ids: tuple[str, ...] | list[str] = (),
        depth: int = 1,
        include_sources: bool = True,
        node_limit: int = DEFAULT_NODE_LIMIT,
        edge_limit: int = DEFAULT_EDGE_LIMIT,
        counterpoints: tuple[str, ...] | list[str] = (),
    ) -> ResearchGraphArtifact:
        if self.store is None:
            return ResearchGraphArtifact(warnings=("Research is not configured",))
        depth = max(0, min(MAX_DEPTH, _as_int(depth, 1)))
        node_limit = max(8, min(200, _as_int(node_limit, DEFAULT_NODE_LIMIT)))
        edge_limit = max(8, min(400, _as_int(edge_limit, DEFAULT_EDGE_LIMIT)))
        focus = _clean_ids(focus_ids)
        warnings: list[str] = []
        if not focus:
            focus = self._default_focus(session_id)
        if not focus:
            return ResearchGraphArtifact(depth=depth, warnings=("No research focus found",))

        note_ids = self._expand_note_ids(focus, depth)
        rows = self.store.index.notes_by_ids(list(note_ids))
        if not rows:
            return ResearchGraphArtifact(depth=depth, warnings=("No graph notes found",))

        existing = {str(row.get("id") or "") for row in rows}
        focus_existing = tuple(note_id for note_id in focus if note_id in existing)
        if not focus_existing:
            focus_existing = (str(rows[0].get("id") or ""),)
            warnings.append("Requested focus was not found; using nearest indexed note")

        note_links = self._note_edges(existing)
        rows = self._trim_note_rows(rows, focus_existing, note_links, node_limit)
        kept = {str(row.get("id") or "") for row in rows}
        kept_focus = tuple(note_id for note_id in focus_existing if note_id in kept)
        note_links = tuple(edge for edge in note_links if edge.src in kept and edge.dst in kept)
        degree = _degree(note_links)

        nodes: list[GraphNode] = [
            self._note_node(row, degree[str(row.get("id") or "")], kept_focus)
            for row in rows
        ]
        edges: list[GraphEdge] = list(note_links)

        if counterpoints and not self._has_real_counterpoint(note_links, focus_existing):
            self._append_counterpoints(
                nodes,
                edges,
                focus_existing[0],
                counterpoints,
                node_limit=node_limit,
                edge_limit=edge_limit,
            )

        if include_sources:
            self._append_source_urls(
                nodes,
                edges,
                kept,
                node_limit=node_limit,
                edge_limit=edge_limit,
            )

        edges = self._trim_edges(edges, focus_existing, edge_limit)
        node_ids = {node.id for node in nodes}
        edges = [edge for edge in edges if edge.src in node_ids and edge.dst in node_ids]
        return ResearchGraphArtifact(
            center_id=focus_existing[0],
            focus_ids=kept_focus,
            depth=depth,
            nodes=tuple(nodes),
            edges=tuple(edges),
            warnings=tuple(warnings),
        )

    def _default_focus(self, session_id: str) -> tuple[str, ...]:
        session_id = str(session_id or "").strip()
        if not session_id:
            return ()
        try:
            rows = self.store.index.recent(
                1,
                session_id=session_id,
                types=("synthesis", "decision"),
            )
        except Exception:
            rows = []
        if not rows:
            try:
                rows = self.store.index.recent(1, session_id=session_id)
            except Exception:
                rows = []
        return tuple(str(row.get("id") or "") for row in rows if row.get("id"))

    def _expand_note_ids(self, focus: tuple[str, ...], depth: int) -> tuple[str, ...]:
        selected: set[str] = set(focus)
        frontier: set[str] = set(focus)
        for _level in range(depth):
            if not frontier:
                break
            rows = self.store.index.links_touching(sorted(frontier))
            next_frontier: set[str] = set()
            for row in rows:
                src = str(row.get("src_id") or "")
                dst = str(row.get("dst_id") or "")
                for note_id in (src, dst):
                    if note_id and note_id not in selected:
                        next_frontier.add(note_id)
            selected.update(next_frontier)
            frontier = next_frontier
        return tuple(focus) + tuple(sorted(selected - set(focus)))

    def _note_edges(self, note_ids: set[str]) -> tuple[GraphEdge, ...]:
        rows = self.store.index.links_touching(sorted(note_ids))
        edges: dict[tuple[str, str, str], GraphEdge] = {}
        for row in rows:
            src = str(row.get("src_id") or "")
            dst = str(row.get("dst_id") or "")
            kind = str(row.get("kind") or "relates")
            if not src or not dst or src not in note_ids or dst not in note_ids:
                continue
            if kind not in GRAPH_EDGE_KINDS:
                kind = "relates"
            edges[(src, dst, kind)] = GraphEdge(src=src, dst=dst, kind=kind, label=kind)
        return tuple(edges.values())

    def _trim_note_rows(
        self,
        rows: list[dict],
        focus_ids: tuple[str, ...],
        edges: tuple[GraphEdge, ...],
        limit: int,
    ) -> list[dict]:
        if len(rows) <= limit:
            return rows
        edge_bonus: dict[str, int] = {}
        degree = Counter()
        for edge in edges:
            bonus = _EDGE_PRIORITY.get(edge.kind, 0)
            degree[edge.src] += 1
            degree[edge.dst] += 1
            edge_bonus[edge.src] = max(edge_bonus.get(edge.src, 0), bonus)
            edge_bonus[edge.dst] = max(edge_bonus.get(edge.dst, 0), bonus)
        focus = set(focus_ids)

        def rank(row: dict) -> tuple:
            note_id = str(row.get("id") or "")
            note_type = str(row.get("type") or "note")
            return (
                1 if note_id in focus else 0,
                _TYPE_PRIORITY.get(note_type, 10),
                edge_bonus.get(note_id, 0),
                degree[note_id],
                str(row.get("updated") or ""),
            )

        return sorted(rows, key=rank, reverse=True)[:limit]

    def _note_node(self, row: dict, degree: int, focus_ids: tuple[str, ...]) -> GraphNode:
        note_id = str(row.get("id") or "")
        note_type = str(row.get("type") or "note") or "note"
        return GraphNode(
            id=note_id,
            label=str(row.get("title") or note_id),
            kind=note_type,
            note_type=note_type,
            status=str(row.get("status") or "active"),
            path=str(row.get("path") or ""),
            weight=1.0 + min(6, degree) * 0.35 + (1.0 if note_id in focus_ids else 0.0),
            focus=note_id in focus_ids,
            excerpt=_excerpt(str(row.get("body") or "")),
        )

    def _append_counterpoints(
        self,
        nodes: list[GraphNode],
        edges: list[GraphEdge],
        center_id: str,
        counterpoints: tuple[str, ...] | list[str],
        *,
        node_limit: int,
        edge_limit: int,
    ) -> None:
        for value in counterpoints:
            if len(nodes) >= node_limit or len(edges) >= edge_limit:
                return
            text = _clean_counterpoint(value)
            if not text:
                continue
            node_id = "counterpoint:" + _stable_hash(text)
            if any(node.id == node_id for node in nodes):
                continue
            nodes.append(
                GraphNode(
                    id=node_id,
                    label=_clip(text, 72),
                    kind="counterpoint",
                    note_type="counterpoint",
                    status="active",
                    weight=1.25,
                    excerpt=text,
                    virtual=True,
                )
            )
            edges.append(
                GraphEdge(
                    src=center_id,
                    dst=node_id,
                    kind="contradicts",
                    label="contradicts",
                    weight=1.15,
                    virtual=True,
                )
            )

    def _append_source_urls(
        self,
        nodes: list[GraphNode],
        edges: list[GraphEdge],
        note_ids: set[str],
        *,
        node_limit: int,
        edge_limit: int,
    ) -> None:
        sources = self.store.index.sources_for(sorted(note_ids))
        existing_nodes = {node.id for node in nodes}
        source_counts = Counter(str(row.get("source") or "") for row in sources)
        for row in sources:
            if len(edges) >= edge_limit:
                return
            note_id = str(row.get("note_id") or "")
            source = str(row.get("source") or "").strip()
            if note_id not in note_ids or not _looks_like_url(source):
                continue
            node_id = "source:" + _stable_hash(source)
            if node_id not in existing_nodes:
                if len(nodes) >= node_limit:
                    continue
                nodes.append(
                    GraphNode(
                        id=node_id,
                        label=_source_label(source),
                        kind="source_url",
                        note_type="source_url",
                        url=source,
                        weight=0.8 + min(5, source_counts[source]) * 0.2,
                        excerpt=source,
                        virtual=True,
                    )
                )
                existing_nodes.add(node_id)
            edge = GraphEdge(
                src=note_id,
                dst=node_id,
                kind="cites",
                label="cites",
                weight=0.75,
                virtual=True,
            )
            if not any(e.src == edge.src and e.dst == edge.dst and e.kind == edge.kind for e in edges):
                edges.append(edge)

    def _has_real_counterpoint(
        self,
        edges: tuple[GraphEdge, ...],
        focus_ids: tuple[str, ...],
    ) -> bool:
        focus = set(focus_ids)
        return any(
            edge.kind == "contradicts" and (edge.src in focus or edge.dst in focus)
            for edge in edges
        )

    def _trim_edges(
        self,
        edges: list[GraphEdge],
        focus_ids: tuple[str, ...],
        limit: int,
    ) -> list[GraphEdge]:
        if len(edges) <= limit:
            return edges
        focus = set(focus_ids)

        def rank(edge: GraphEdge) -> tuple:
            return (
                1 if edge.src in focus or edge.dst in focus else 0,
                _EDGE_PRIORITY.get(edge.kind, 0),
                edge.weight,
                edge.src,
                edge.dst,
            )

        return sorted(edges, key=rank, reverse=True)[:limit]


def _degree(edges: tuple[GraphEdge, ...]) -> Counter:
    degree = Counter()
    for edge in edges:
        degree[edge.src] += 1
        degree[edge.dst] += 1
    return degree


def _clean_ids(values: tuple[str, ...] | list[str]) -> tuple[str, ...]:
    out: list[str] = []
    seen: set[str] = set()
    for value in values:
        item = str(value or "").strip()
        if item and item not in seen:
            seen.add(item)
            out.append(item)
    return tuple(out)


def _as_int(value: object, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _looks_like_url(value: str) -> bool:
    return value.lower().startswith(("http://", "https://"))


def _source_label(url: str) -> str:
    parsed = urlparse(url)
    host = parsed.netloc or url
    tail = parsed.path.rstrip("/").rsplit("/", 1)[-1]
    if tail and tail not in {host, ""}:
        return _clip(f"{host}/{tail}", 58)
    return _clip(host, 58)


def _stable_hash(value: str) -> str:
    return hashlib.sha1(value.encode("utf-8")).hexdigest()[:16]


def _excerpt(body: str, limit: int = 260) -> str:
    lines = []
    for line in str(body or "").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        lines.append(stripped)
        if len(" ".join(lines)) >= limit:
            break
    return _clip(" ".join(lines), limit)


def _clean_counterpoint(value: object) -> str:
    text = re.sub(r"^\s*[-*]\s*", "", str(value or "").strip())
    text = re.sub(r"\s+", " ", text)
    return text


def _clip(value: str, limit: int) -> str:
    text = str(value or "").strip()
    if len(text) <= limit:
        return text
    return text[: max(1, limit - 3)].rstrip() + "..."
