"""A local graph of the exact versions used by a research session."""

from __future__ import annotations

from collections import deque
from typing import Any

from codey.knowledge.graph import GraphEdge, GraphNode, ResearchGraphArtifact
from codey.knowledge.model import KnowledgeVersion
from codey.knowledge.recall import KnowledgeAccess, KnowledgeRecall
from codey.knowledge.store import KnowledgeStore


def version_node_id(identity: str, revision: int) -> str:
    return f"knowledge:{identity}@{revision}"


def version_detail(version: KnowledgeVersion) -> str:
    value = version.value
    lines = [f"{value.kind} · {value.status} · version {version.revision}",
             f"Origin: {value.origin}", f"Recorded: {version.recorded_at}", "", value.content[:5000]]
    if version.needs_review:
        lines.append("\nNeeds review: this version or one of its premises has changed or been withdrawn.")
    lines.append("\nConditions:")
    lines.extend(f"- {name}: {condition.get('value', '')} ({condition['state']})" for name, condition in value.conditions.items())
    if not value.conditions:
        lines.append("- Not extracted; absence does not mean unrestricted applicability.")
    lines.append("\nProvenance:")
    for key in ("url", "locator", "page", "retrieved_at", "study_id", "independence", "basis", "interpretation"):
        if value.provenance.get(key) is not None:
            lines.append(f"- {key}: {value.provenance[key]}")
    lines.append("\nPinned references:")
    lines.extend(f"- {ref.relation}: {ref.target} (version {ref.revision})" for ref in value.references)
    lines.append("\nA connection is not proof. Candidate relationships remain hypotheses.")
    return "\n".join(lines)


def _focus_versions(store: KnowledgeStore, focus_ids: tuple[str, ...] | list[str]) -> list[tuple[str, int]]:
    seeds: list[tuple[str, int]] = []
    for identity in focus_ids:
        if identity.startswith("knowledge:") and "@" in identity:
            raw_id, raw_revision = identity.removeprefix("knowledge:").rsplit("@", 1)
            if raw_revision.isdecimal():
                seeds.append((raw_id, int(raw_revision)))
        elif store.repository.read(identity):
            version = store.repository.read(identity)
            if version:
                seeds.append((identity, version.revision))
        else:
            seeds.extend((str(row["target"]), int(row["revision"])) for row in store.note_references(identity))
    return seeds


def build_version_graph(store: KnowledgeStore, session_id: str, *, focus_ids: tuple[str, ...] | list[str] = (),
                        depth: int = 1, node_limit: int = 96, edge_limit: int = 192) -> ResearchGraphArtifact | None:
    conn = store.index._read_conn()
    # Session membership is a projection; the payload and evidence remain owned by
    # the repository. This query never mixes unrelated private projects.
    notes: list[dict[str, Any]] = store.index.recent(24, session_id=session_id) if session_id else []
    project = next((str(note["project"]) for note in notes if note.get("project")), "")
    if not project and session_id:
        projects = conn.execute("SELECT DISTINCT project FROM knowledge_commit_context WHERE session_id=? AND project!='' LIMIT 2",
                                (session_id,)).fetchall()
        project = str(projects[0][0]) if len(projects) == 1 else ""
    depth, node_limit, edge_limit = max(0, min(3, depth)), max(1, min(200, node_limit)), max(0, min(400, edge_limit))
    access = KnowledgeAccess(project, session_id, store.owner_id)
    seeds: list[tuple[str, int]] = []
    seeds.extend(_focus_versions(store, focus_ids))
    for note in notes:
        seeds.extend((str(row["target"]), int(row["revision"])) for row in store.note_references(str(note["id"])))
    if session_id:
        rows = conn.execute("SELECT v.id,v.revision FROM knowledge_versions v "
                            "JOIN knowledge_commit_context c ON c.commit_id=v.commit_id "
                            "WHERE c.session_id=? ORDER BY v.recorded_at DESC LIMIT 64", (session_id,)).fetchall()
        seeds.extend((str(row[0]), int(row[1])) for row in rows)
    if not seeds:
        return None
    versions: dict[tuple[str, int], KnowledgeVersion] = {}
    for identity, revision in dict.fromkeys(seeds):
        version = store.repository.read(identity, revision)
        if version and access.allows(version):
            versions[(identity, revision)] = version
    if not versions:
        return ResearchGraphArtifact(warnings=("No accessible versions in the requested scope.",))
    priority = {"answer": 0, "inference": 1, "claim": 2, "question": 3, "entity": 4, "evidence": 5, "source": 6}
    ranked = sorted(versions.items(), key=lambda row: priority.get(row[1].value.kind, 7))
    focus = {key for key, _version in ranked[:max(1, node_limit // 3)]}
    queue = deque((version, 0) for _key, version in ranked[:node_limit])
    kept: dict[tuple[str, int], KnowledgeVersion] = {}
    recall = KnowledgeRecall(store.repository)
    while queue and len(kept) < node_limit:
        version, level = queue.popleft()
        key = (version.value.id, version.revision)
        if key in kept or not access.allows(version):
            continue
        kept[key] = version
        if level >= depth:
            continue
        for identity, revision, _relation in recall.neighbors(version, access, limit=12):
            target = store.repository.read(identity, revision)
            if target:
                queue.append((target, level + 1))
    nodes: list[GraphNode] = []
    edges: list[GraphEdge] = []
    for key, version in kept.items():
        value = version.value
        nodes.append(GraphNode(id=version_node_id(*key), label=value.title or value.content[:80],
            kind={"entity": "concept", "answer": "synthesis", "inference": "hypothesis"}.get(value.kind, value.kind),
            note_type=value.kind, status="needs_review" if version.needs_review else value.status,
            focus=key in focus, excerpt=version_detail(version), virtual=True,
            url=str(value.provenance.get("url") or "")))
        for ref in value.references:
            if (ref.target, ref.revision) not in kept or len(edges) >= edge_limit:
                continue
            target = kept[(ref.target, ref.revision)]
            detail = (f"Relation: {ref.relation}\n\nFrom version {version.revision} to version {ref.revision}\n\n"
                      + version_detail(version) + "\n\nTarget:\n\n" + version_detail(target))
            edges.append(GraphEdge(src=version_node_id(*key), dst=version_node_id(ref.target, ref.revision),
                                    kind=ref.relation, virtual=value.status == "candidate", detail=detail))
    focus_ids_out = tuple(node.id for node in nodes if node.focus)
    return ResearchGraphArtifact(center_id=focus_ids_out[0] if focus_ids_out else nodes[0].id,
        focus_ids=focus_ids_out, depth=depth, nodes=tuple(nodes), edges=tuple(edges),
        warnings=("Candidate links are hypotheses; relation counts are not evidence strength.",))
