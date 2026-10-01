"""Affinity events: construction, validation, and single-transition replay.

Owns event shapes and the deterministic ``rows <- events`` projection.
Pure over already-loaded payloads: no file locks, no atomic writes, no
model calls. The affinity store owns persistence and transactions and
delegates here. All transitions go through one ``apply_affinity_event``
implementation so validation and state updates cannot drift.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterable, Mapping
from typing import Any

from codey.ghost import _common
from codey.ghost.affinity_model import (
    EDGE_HALF_LIFE_DAYS,
    EDGE_LEARNING_RATE,
    MAX_AFFINITY_EDGES,
    MAX_AFFINITY_NODES,
    MAX_AFFINITY_REFS,
    MAX_EDGE_OUT_DEGREE,
    MIN_EDGE_WEIGHT,
    MIN_NODE_WEIGHT,
    NODE_HALF_LIFE_DAYS,
    NODE_LEARNING_RATE,
    AffinityEdge,
    AffinityEdgeSpec,
    AffinityNode,
    AffinityNodeSpec,
    _bounded_ref_hashes,
    _bounded_refs,
    _bounded_warnings,
    _clean_key,
    _clean_label,
    _clean_metadata,
    _clean_node_kind,
    _clean_relation,
    _clean_scope,
    _edge_id,
    _edge_spec_from_payload,
    _edge_spec_payload,
    _list,
    _merge_ref_hashes,
    _merge_refs,
    _node_id,
    _node_spec_from_payload,
    _node_spec_payload,
    _ref_hash,
    _unit_float,
    _valid_affinity_edge_payload,
    _valid_affinity_node_payload,
    _valid_edge_spec_payload,
    _valid_node_spec_payload,
)
from codey.ghost.graph_primitives import (
    any_decay_due as _shared_any_decay_due,
)
from codey.ghost.graph_primitives import (
    bound_graph_edges as _shared_bound_graph_edges,
)
from codey.ghost.graph_primitives import (
    bound_graph_nodes as _shared_bound_graph_nodes,
)
from codey.ghost.graph_primitives import (
    decay_basis_of as _shared_decay_basis_of,
)
from codey.ghost.graph_primitives import (
    decayed_by_half_life as _shared_decayed_by_half_life,
)
from codey.ghost.schema import clip_signal_text

_AFFINITY_EVENT_TYPES = frozenset(
    {
        "ghost_affinity_node_reinforced",
        "ghost_affinity_edge_reinforced",
        "ghost_affinity_scope_deleted",
        "ghost_affinity_decay_applied",
        "ghost_affinity_snapshot",
    }
)
_SCOPE_DELETED_PAYLOAD_KEYS = frozenset({"scope", "scope_ref", "removed_nodes", "removed_edges"})
_DECAY_PAYLOAD_KEYS = frozenset(
    {"removed_nodes", "removed_edges", "decayed_nodes", "decayed_edges", "min_interval_seconds"}
)
_AFFINITY_EVENT_KEYS = {
    "ghost_affinity_node_reinforced": frozenset({"schema_version", "type", "event_id", "ts", "spec"}),
    "ghost_affinity_edge_reinforced": frozenset({"schema_version", "type", "event_id", "ts", "spec"}),
    "ghost_affinity_scope_deleted": frozenset({"schema_version", "type", "event_id", "ts", "payload"}),
    "ghost_affinity_decay_applied": frozenset({"schema_version", "type", "event_id", "ts", "payload"}),
    "ghost_affinity_snapshot": frozenset({"schema_version", "type", "event_id", "ts", "reason", "nodes", "edges"}),
}


def apply_affinity_event(state: dict[str, dict[str, Any]], event: Mapping[str, object]) -> None:
    """Validate and apply one event; illegal events raise explicitly."""
    if not isinstance(event, Mapping):
        raise ValueError("affinity event must be a mapping")
    if not _valid_affinity_event(event):
        raise ValueError("invalid affinity event envelope")
    event_type = str(event.get("type") or "")
    now = _common.event_ts(event)
    nodes: dict[str, AffinityNode] = state.setdefault("nodes", {})
    edges: dict[str, AffinityEdge] = state.setdefault("edges", {})
    if event_type == "ghost_affinity_snapshot":
        snapshot_nodes, snapshot_edges = _snapshot_rows(event)
        state["nodes"] = {node.id: node for node in snapshot_nodes}
        state["edges"] = {edge.id: edge for edge in snapshot_edges}
        return
    if event_type == "ghost_affinity_node_reinforced":
        spec = _node_spec_from_payload(event.get("spec"))
        if spec is None:
            raise ValueError("invalid affinity node spec")
        node, changed = _reinforce_node(
            nodes.get(_node_id(spec.kind, spec.scope, spec.scope_ref, spec.key)),
            spec,
            now=now,
        )
        if changed:
            nodes[node.id] = node
        return
    if event_type == "ghost_affinity_edge_reinforced":
        spec = _edge_spec_from_payload(event.get("spec"))
        if spec is None:
            raise ValueError("invalid affinity edge spec")
        if spec.source not in nodes or spec.target not in nodes:
            raise ValueError("orphan affinity edge")
        edge, changed = _reinforce_edge(
            edges.get(_edge_id(spec.source, spec.target, spec.relation, spec.scope, spec.scope_ref)),
            spec,
            now=now,
        )
        if changed:
            edges[edge.id] = edge
        return
    if event_type == "ghost_affinity_scope_deleted":
        payload = event.get("payload")
        if not isinstance(payload, Mapping):
            raise ValueError("invalid scope-deleted payload")
        kept_nodes, kept_edges, _removed_nodes, _removed_edges = _delete_scope_rows(
            nodes.values(),
            edges.values(),
            normalized_scope=_clean_scope(payload.get("scope")),
            scope_ref=clip_signal_text(payload.get("scope_ref"), 120),
        )
        state["nodes"] = {node.id: node for node in kept_nodes}
        state["edges"] = {edge.id: edge for edge in kept_edges}
        return
    if event_type == "ghost_affinity_decay_applied":
        decayed_nodes = [_decay_node(node, now=now) for node in nodes.values()]
        bounded_nodes = _bounded_nodes(decayed_nodes)
        decayed_edges = [_decay_edge(edge, now=now) for edge in edges.values()]
        bounded_edges = _bounded_edges(decayed_edges, node_ids={node.id for node in bounded_nodes})
        state["nodes"] = {node.id: node for node in bounded_nodes}
        state["edges"] = {edge.id: edge for edge in bounded_edges}
        return
    raise ValueError(f"unknown affinity event type {event_type!r}")


def replay_affinity_events(
    events: Iterable[dict[str, object]],
) -> tuple[list[AffinityNode], list[AffinityEdge]]:
    """Replay through the single apply function and return nodes and edges."""
    state: dict[str, dict[str, Any]] = {"nodes": {}, "edges": {}}
    for event in events:
        apply_affinity_event(state, event)
    nodes = _bounded_nodes(state["nodes"].values())
    edges = _bounded_edges(state["edges"].values(), node_ids={node.id for node in nodes})
    return nodes, edges


def _reinforce_node(
    current: AffinityNode | None, spec: AffinityNodeSpec, *, now: str
) -> tuple[AffinityNode, bool]:
    kind = _clean_node_kind(spec.kind)
    scope = _clean_scope(spec.scope)
    key = _clean_key(spec.key, 180)
    label = _clean_label(spec.label, 180)
    scope_ref = clip_signal_text(spec.scope_ref, 120)
    if not kind or not scope or not key or not label:
        raise ValueError("invalid affinity node")
    node_id = _node_id(kind, scope, scope_ref, key)
    source_refs = _bounded_refs(spec.source_refs)
    evidence_refs = _bounded_refs(spec.evidence_refs)
    source_ref_hashes = _bounded_ref_hashes(source_refs)
    evidence_ref_hashes = _bounded_ref_hashes(evidence_refs)
    if not source_refs and not evidence_refs:
        raise ValueError("affinity node requires bounded source refs")
    if current is not None:
        known_source_hashes = set(current.source_ref_hashes or _bounded_ref_hashes(current.source_refs))
        known_evidence_hashes = set(current.evidence_ref_hashes or _bounded_ref_hashes(current.evidence_refs))
        new_source_refs = tuple(ref for ref in source_refs if _ref_hash(ref) not in known_source_hashes)
        new_evidence_refs = tuple(ref for ref in evidence_refs if _ref_hash(ref) not in known_evidence_hashes)
        if not new_source_refs and not new_evidence_refs and current.status == "active":
            return current, False
        old_weight = _decayed_weight(current.weight, _decay_basis(current), now, NODE_HALF_LIFE_DAYS)
        created_at = current.created_at or now
        source_refs = _merge_refs(current.source_refs, new_source_refs, limit=MAX_AFFINITY_REFS)
        evidence_refs = _merge_refs(current.evidence_refs, new_evidence_refs, limit=MAX_AFFINITY_REFS)
        source_ref_hashes = _merge_ref_hashes(current.source_ref_hashes or current.source_refs, new_source_refs)
        evidence_ref_hashes = _merge_ref_hashes(current.evidence_ref_hashes or current.evidence_refs, new_evidence_refs)
        confidence = max(current.confidence, _unit_float(spec.confidence))
        metadata = _clean_metadata({**dict(current.metadata), **dict(spec.metadata)})
    else:
        old_weight = 0.0
        created_at = now
        confidence = _unit_float(spec.confidence)
        metadata = _clean_metadata(spec.metadata)
    increment = NODE_LEARNING_RATE * _unit_float(spec.reward) * max(confidence, 0.1)
    node = AffinityNode(
        id=node_id,
        kind=kind,
        key=key,
        label=label,
        scope=scope,
        scope_ref=scope_ref,
        status="active",
        weight=_unit_float(old_weight + increment),
        confidence=confidence,
        source_refs=source_refs,
        evidence_refs=evidence_refs,
        source_ref_hashes=source_ref_hashes,
        evidence_ref_hashes=evidence_ref_hashes,
        metadata=metadata,
        created_at=created_at,
        updated_at=now,
        last_reinforced_at=now,
        last_decayed_at=now,
    )
    return node, True


def _reinforce_edge(
    current: AffinityEdge | None, spec: AffinityEdgeSpec, *, now: str
) -> tuple[AffinityEdge, bool]:
    relation = _clean_relation(spec.relation)
    scope = _clean_scope(spec.scope)
    scope_ref = clip_signal_text(spec.scope_ref, 120)
    source = clip_signal_text(spec.source, 120)
    target = clip_signal_text(spec.target, 120)
    if relation == "associated_with":
        source, target = sorted((source, target))
    if not relation or not scope or not source or not target or source == target:
        raise ValueError("invalid affinity edge")
    edge_id = _edge_id(source, target, relation, scope, scope_ref)
    source_refs = _bounded_refs(spec.source_refs)
    proof_refs = _bounded_refs(spec.proof_refs)
    source_ref_hashes = _bounded_ref_hashes(source_refs)
    proof_ref_hashes = _bounded_ref_hashes(proof_refs)
    if not source_refs and not proof_refs:
        raise ValueError("affinity edge requires bounded source refs")
    if current is not None:
        known_source_hashes = set(current.source_ref_hashes or _bounded_ref_hashes(current.source_refs))
        known_proof_hashes = set(current.proof_ref_hashes or _bounded_ref_hashes(current.proof_refs))
        new_source_refs = tuple(ref for ref in source_refs if _ref_hash(ref) not in known_source_hashes)
        new_proof_refs = tuple(ref for ref in proof_refs if _ref_hash(ref) not in known_proof_hashes)
        if not new_source_refs and not new_proof_refs and current.status == "active":
            return current, False
        old_weight = _decayed_weight(current.weight, _decay_basis(current), now, EDGE_HALF_LIFE_DAYS)
        created_at = current.created_at or now
        source_refs = _merge_refs(current.source_refs, new_source_refs, limit=MAX_AFFINITY_REFS)
        proof_refs = _merge_refs(current.proof_refs, new_proof_refs, limit=MAX_AFFINITY_REFS)
        source_ref_hashes = _merge_ref_hashes(current.source_ref_hashes or current.source_refs, new_source_refs)
        proof_ref_hashes = _merge_ref_hashes(current.proof_ref_hashes or current.proof_refs, new_proof_refs)
        confidence = max(current.confidence, _unit_float(spec.confidence))
    else:
        old_weight = 0.0
        created_at = now
        confidence = _unit_float(spec.confidence)
    increment = EDGE_LEARNING_RATE * _unit_float(spec.reward) * max(confidence, 0.1)
    edge = AffinityEdge(
        id=edge_id,
        source=source,
        target=target,
        relation=relation,
        scope=scope,
        scope_ref=scope_ref,
        status="active",
        weight=_unit_float(old_weight + increment),
        confidence=confidence,
        source_refs=source_refs,
        proof_refs=proof_refs,
        source_ref_hashes=source_ref_hashes,
        proof_ref_hashes=proof_ref_hashes,
        created_at=created_at,
        updated_at=now,
        last_reinforced_at=now,
        last_decayed_at=now,
    )
    return edge, True


def _decay_node(node: AffinityNode, *, now: str) -> AffinityNode:
    from dataclasses import replace

    decayed = _decayed_weight(node.weight, _decay_basis(node), now, NODE_HALF_LIFE_DAYS)
    status = "expired" if node.status == "active" and decayed < MIN_NODE_WEIGHT else node.status
    return replace(node, weight=decayed, status=status, updated_at=now, last_decayed_at=now)


def _decay_edge(edge: AffinityEdge, *, now: str) -> AffinityEdge:
    from dataclasses import replace

    decayed = _decayed_weight(edge.weight, _decay_basis(edge), now, EDGE_HALF_LIFE_DAYS)
    status = "expired" if decayed < MIN_EDGE_WEIGHT else edge.status
    return replace(edge, weight=decayed, status=status, updated_at=now, last_decayed_at=now)


def _decay_basis(row: AffinityNode | AffinityEdge) -> str:
    return _shared_decay_basis_of(row.last_decayed_at, row.last_reinforced_at, row.updated_at)


def _decayed_weight(weight: float, basis: str, now: str, half_life_days: float) -> float:
    return _unit_float(_shared_decayed_by_half_life(weight, basis, now, half_life_days))


def _any_decay_due(
    rows: Iterable[AffinityNode | AffinityEdge],
    *,
    now: str,
    min_interval_seconds: int,
) -> bool:
    return _shared_any_decay_due(
        (_decay_basis(row) for row in rows),
        now=now,
        min_interval_seconds=min_interval_seconds,
    )


def _bounded_nodes(nodes: Iterable[AffinityNode]) -> list[AffinityNode]:
    return _shared_bound_graph_nodes(
        nodes, min_active_weight=MIN_NODE_WEIGHT, limit=MAX_AFFINITY_NODES
    )


def _bounded_edges(edges: Iterable[AffinityEdge], *, node_ids: set[str]) -> list[AffinityEdge]:
    return _shared_bound_graph_edges(
        edges,
        node_ids=node_ids,
        min_weight=MIN_EDGE_WEIGHT,
        limit=MAX_AFFINITY_EDGES,
        max_out_degree=MAX_EDGE_OUT_DEGREE,
        require_active=True,
    )


def _snapshot_rows(event: Mapping[str, object]) -> tuple[list[AffinityNode], list[AffinityEdge]]:
    nodes = [node for node in (AffinityNode.from_payload(row) for row in _list(event.get("nodes"))) if node is not None]
    node_ids = {node.id for node in nodes}
    edges = [
        edge
        for edge in (AffinityEdge.from_payload(row) for row in _list(event.get("edges")))
        if edge is not None and edge.source in node_ids and edge.target in node_ids
    ]
    return _bounded_nodes(nodes), _bounded_edges(edges, node_ids=node_ids)


def _delete_scope_rows(
    nodes: Iterable[AffinityNode],
    edges: Iterable[AffinityEdge],
    *,
    normalized_scope: str,
    scope_ref: str,
) -> tuple[list[AffinityNode], list[AffinityEdge], int, int]:
    node_rows = list(nodes)
    edge_rows = list(edges)
    removed_node_ids = {
        node.id
        for node in node_rows
        if node.scope == normalized_scope and (normalized_scope == "user" or node.scope_ref == scope_ref)
    }
    kept_nodes = [node for node in node_rows if node.id not in removed_node_ids]
    kept_node_ids = {node.id for node in kept_nodes}
    kept_edges = [
        edge
        for edge in edge_rows
        if edge.source in kept_node_ids
        and edge.target in kept_node_ids
        and not (edge.scope == normalized_scope and (normalized_scope == "user" or edge.scope_ref == scope_ref))
    ]
    removed_edges = len(edge_rows) - len(kept_edges)
    bounded_nodes = _bounded_nodes(kept_nodes)
    bounded_edges = _bounded_edges(kept_edges, node_ids={node.id for node in bounded_nodes})
    return bounded_nodes, bounded_edges, len(removed_node_ids), removed_edges


def _projection_payload(
    nodes: Iterable[AffinityNode],
    edges: Iterable[AffinityEdge],
    *,
    generated_at: str,
    warnings: Iterable[str],
) -> dict[str, object]:
    from codey.ghost.affinity_model import _STATE_KIND

    node_rows = _bounded_nodes(nodes)
    edge_rows = _bounded_edges(edges, node_ids={node.id for node in node_rows})
    return {
        "schema_version": __import__("codey.ghost.affinity_model", fromlist=["AFFINITY_SCHEMA_VERSION"]).AFFINITY_SCHEMA_VERSION,
        "kind": _STATE_KIND,
        "source": "affinity_events.jsonl",
        "generated_at": generated_at,
        "nodes": [node.to_payload() for node in node_rows],
        "edges": [edge.to_payload() for edge in edge_rows],
        "warnings": list(_bounded_warnings(warnings)),
    }


def _node_reinforced_event(spec: AffinityNodeSpec, *, ts: str) -> dict[str, object]:
    from codey.ghost.affinity_model import AFFINITY_SCHEMA_VERSION

    return {
        "schema_version": AFFINITY_SCHEMA_VERSION,
        "type": "ghost_affinity_node_reinforced",
        "event_id": "gae_" + uuid.uuid4().hex[:24],
        "ts": clip_signal_text(ts, 80),
        "spec": _node_spec_payload(spec),
    }


def _edge_reinforced_event(spec: AffinityEdgeSpec, *, ts: str) -> dict[str, object]:
    from codey.ghost.affinity_model import AFFINITY_SCHEMA_VERSION

    return {
        "schema_version": AFFINITY_SCHEMA_VERSION,
        "type": "ghost_affinity_edge_reinforced",
        "event_id": "gae_" + uuid.uuid4().hex[:24],
        "ts": clip_signal_text(ts, 80),
        "spec": _edge_spec_payload(spec),
    }


def _scope_deleted_event(
    scope: str,
    scope_ref: str,
    *,
    removed_nodes: int,
    removed_edges: int,
    ts: str,
) -> dict[str, object]:
    from codey.ghost.affinity_model import AFFINITY_SCHEMA_VERSION
    from codey.ghost.affinity_model import _clean_scope as _clean_scope_value

    return {
        "schema_version": AFFINITY_SCHEMA_VERSION,
        "type": "ghost_affinity_scope_deleted",
        "event_id": "gac_" + uuid.uuid4().hex[:24],
        "ts": clip_signal_text(ts, 80),
        "payload": {
            "scope": _clean_scope_value(scope),
            "scope_ref": clip_signal_text(scope_ref, 120),
            "removed_nodes": max(0, int(removed_nodes or 0)),
            "removed_edges": max(0, int(removed_edges or 0)),
        },
    }


def _decay_applied_event(
    *,
    removed_nodes: int,
    removed_edges: int,
    decayed_nodes: int,
    decayed_edges: int,
    min_interval_seconds: int,
    ts: str,
) -> dict[str, object]:
    from codey.ghost.affinity_model import AFFINITY_SCHEMA_VERSION

    return {
        "schema_version": AFFINITY_SCHEMA_VERSION,
        "type": "ghost_affinity_decay_applied",
        "event_id": "gac_" + uuid.uuid4().hex[:24],
        "ts": clip_signal_text(ts, 80),
        "payload": {
            "removed_nodes": max(0, int(removed_nodes or 0)),
            "removed_edges": max(0, int(removed_edges or 0)),
            "decayed_nodes": max(0, int(decayed_nodes or 0)),
            "decayed_edges": max(0, int(decayed_edges or 0)),
            "min_interval_seconds": max(0, int(min_interval_seconds or 0)),
        },
    }


def _snapshot_event(
    nodes: Iterable[AffinityNode],
    edges: Iterable[AffinityEdge],
    *,
    ts: str,
    reason: str,
) -> dict[str, object]:
    from codey.ghost.affinity_model import AFFINITY_SCHEMA_VERSION

    node_rows = _bounded_nodes(nodes)
    edge_rows = _bounded_edges(edges, node_ids={node.id for node in node_rows})
    return {
        "schema_version": AFFINITY_SCHEMA_VERSION,
        "type": "ghost_affinity_snapshot",
        "event_id": "gas_" + uuid.uuid4().hex[:24],
        "ts": clip_signal_text(ts, 80),
        "reason": clip_signal_text(reason, 80),
        "nodes": [node.to_payload() for node in node_rows],
        "edges": [edge.to_payload() for edge in edge_rows],
    }


def _valid_affinity_event(event: Mapping[str, object]) -> bool:
    from codey.ghost.affinity_model import AFFINITY_SCHEMA_VERSION

    if not clip_signal_text(event.get("event_id"), 120):
        return False
    if not clip_signal_text(event.get("ts"), 80):
        return False
    event_type = str(event.get("type") or "")
    if event_type not in _AFFINITY_EVENT_TYPES:
        return False
    if event.get("schema_version") != AFFINITY_SCHEMA_VERSION:
        return False
    expected = _AFFINITY_EVENT_KEYS.get(event_type)
    if expected is None or set(event.keys()) != set(expected):
        return False
    if event_type == "ghost_affinity_snapshot":
        return _valid_affinity_snapshot(event)
    if event_type == "ghost_affinity_node_reinforced":
        return _valid_node_spec_payload(event.get("spec"))
    if event_type == "ghost_affinity_edge_reinforced":
        return _valid_edge_spec_payload(event.get("spec"))
    if event_type == "ghost_affinity_scope_deleted":
        return _valid_scope_deleted_payload(event.get("payload"))
    if event_type == "ghost_affinity_decay_applied":
        return _valid_decay_payload(event.get("payload"))
    return False


def _valid_affinity_snapshot(event: Mapping[str, object]) -> bool:
    if not isinstance(event.get("reason"), str):
        return False
    raw_nodes = event.get("nodes")
    raw_edges = event.get("edges")
    if not isinstance(raw_nodes, list) or not isinstance(raw_edges, list):
        return False
    if len(raw_nodes) > MAX_AFFINITY_NODES or len(raw_edges) > MAX_AFFINITY_EDGES:
        return False
    nodes: list[AffinityNode] = []
    node_ids: set[str] = set()
    for row in raw_nodes:
        node = AffinityNode.from_payload(row)
        if node is None or node.id in node_ids:
            return False
        if not _valid_affinity_node_payload(row):
            return False
        nodes.append(node)
        node_ids.add(node.id)
    edge_ids: set[str] = set()
    for row in raw_edges:
        edge = AffinityEdge.from_payload(row)
        if edge is None or edge.id in edge_ids:
            return False
        if not _valid_affinity_edge_payload(row):
            return False
        if edge.source not in node_ids or edge.target not in node_ids:
            return False
        edge_ids.add(edge.id)
    return True


def _affinity_events_replay_cleanly(events: Iterable[dict[str, object]]) -> bool:
    try:
        replay_affinity_events(events)
    except (ValueError, TypeError, AttributeError):
        return False
    return True


def _valid_scope_deleted_payload(payload: object) -> bool:
    from codey.ghost.affinity_model import _clean_scope as _clean_scope_value
    from codey.ghost.schema import contains_sensitive_signal_text

    if not isinstance(payload, Mapping) or set(payload.keys()) != _SCOPE_DELETED_PAYLOAD_KEYS:
        return False
    scope = payload.get("scope")
    scope_ref = payload.get("scope_ref")
    if not isinstance(scope, str) or _clean_scope_value(scope) != scope:
        return False
    if not isinstance(scope_ref, str) or clip_signal_text(scope_ref, 120) != scope_ref:
        return False
    if contains_sensitive_signal_text(scope_ref):
        return False
    if not _common.valid_nonnegative_int_payload(payload.get("removed_nodes")):
        return False
    if not _common.valid_nonnegative_int_payload(payload.get("removed_edges")):
        return False
    if scope == "user":
        return scope_ref == ""
    return bool(scope_ref)


def _valid_decay_payload(payload: object) -> bool:
    if not isinstance(payload, Mapping):
        return False
    if set(payload.keys()) != _DECAY_PAYLOAD_KEYS:
        return False
    return all(_common.valid_nonnegative_int_payload(payload.get(key)) for key in _DECAY_PAYLOAD_KEYS)


__all__ = [
    "apply_affinity_event",
    "replay_affinity_events",
]
