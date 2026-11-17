"""Bounded local Hebbian state for accepted Ghost memory candidates."""

from __future__ import annotations

import hashlib
import math
import uuid
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, replace
from pathlib import Path

from codey.ghost import _common
from codey.ghost.events import (
    compact_result_payload as _shared_compact_payload,
)
from codey.ghost.events import (
    control_event as _ghost_control_event,
)
from codey.ghost.events import (
    over_compact_budget,
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
from codey.ghost.graph_primitives import parse_ts
from codey.ghost.graph_store import GhostGraph
from codey.ghost.inbox import GhostInboxStore, GhostMemoryCandidate
from codey.ghost.numbers import coerce_unit_float
from codey.ghost.payload_types import object_fields
from codey.ghost.schema import SIGNAL_KINDS, SIGNAL_SCOPES, clip_signal_text
from codey.ghost.sqlite import GhostSQLite
from codey.runtime.core import cancellation
from codey.storage.local_store import (
    DEFAULT_STATE_HOME,
)
from codey.utils.refs import coerce_float, coerce_int

HEBBIAN_SCHEMA_VERSION = 1


def _safe_count(value: object) -> int:
    if isinstance(value, bool):
        return 0
    try:
        return max(0, coerce_int(value or 0))
    except (TypeError, ValueError, OverflowError):
        return 0


MAX_GHOST_NODES = 500
MAX_GHOST_EDGES = 2_000
MAX_HEBBIAN_EVENTS = 5_000
MAX_HEBBIAN_EVENTS_BYTES = 4 * 1024 * 1024
MAX_NODE_EVIDENCE_REFS = 32
MAX_EDGE_EVIDENCE_REFS = 32
NODE_LEARNING_RATE = 0.25
EDGE_LEARNING_RATE = 0.15
NODE_HALF_LIFE_DAYS = 90.0
EDGE_HALF_LIFE_DAYS = 180.0
MIN_NODE_WEIGHT = 0.05
MIN_EDGE_WEIGHT = 0.01
MAX_EDGE_OUT_DEGREE = 8


NODE_KINDS = SIGNAL_KINDS
NODE_STATUSES = ("active", "superseded", "expired")
EDGE_RELATIONS = ("coactivated_with",)
_STATE_KIND = "ghost_hebbian_state_projection"
_REPLAY_EVENT_TYPES = frozenset({
    "ghost_hebbian_node_upsert", "ghost_hebbian_node_superseded", "ghost_hebbian_edge_upsert",
    "ghost_hebbian_scope_deleted", "ghost_hebbian_candidate_removed", "ghost_hebbian_state_decayed",
    "ghost_hebbian_projection_pruned",
})


@dataclass(frozen=True)
class GhostNode:
    id: str
    kind: str
    label: str
    conflict_key: str
    value_key: str
    status: str
    scope: str
    scope_ref: str
    weight: float
    confidence: float
    candidate_ids: tuple[str, ...]
    evidence_refs: tuple[str, ...]
    created_at: str
    updated_at: str
    last_reinforced_at: str
    last_decayed_at: str = ""
    superseded_by: str = ""

    def to_payload(self) -> dict[str, object]:
        return {
            "id": self.id,
            "kind": self.kind,
            "label": self.label,
            "conflict_key": self.conflict_key,
            "value_key": self.value_key,
            "status": self.status,
            "scope": self.scope,
            "scope_ref": self.scope_ref,
            "weight": self.weight,
            "confidence": self.confidence,
            "candidate_ids": list(self.candidate_ids),
            "evidence_refs": list(self.evidence_refs),
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "last_reinforced_at": self.last_reinforced_at,
            "last_decayed_at": self.last_decayed_at,
            "superseded_by": self.superseded_by,
        }

    @classmethod
    def from_payload(cls, payload: object) -> GhostNode | None:
        if not object_fields(payload) or not _valid_recorded_times(payload):
            return None
        kind = str(payload.get("kind") or "").strip().lower()
        status = str(payload.get("status") or "").strip().lower()
        scope = str(payload.get("scope") or "").strip().lower()
        if kind not in NODE_KINDS or status not in NODE_STATUSES or scope not in SIGNAL_SCOPES:
            return None
        node_id = clip_signal_text(payload.get("id"), 120)
        label = clip_signal_text(payload.get("label"))
        conflict_key = clip_signal_text(payload.get("conflict_key"), 180)
        value_key = clip_signal_text(payload.get("value_key"), 180)
        if not node_id or not label or not conflict_key or not value_key:
            return None
        weight = _coerce_weight(payload.get("weight"))
        confidence = _coerce_confidence(payload.get("confidence"))
        if weight is None or confidence is None:
            return None
        return cls(
            id=node_id,
            kind=kind,
            label=label,
            conflict_key=conflict_key,
            value_key=value_key,
            status=status,
            scope=scope,
            scope_ref=clip_signal_text(payload.get("scope_ref"), 240),
            weight=weight,
            confidence=confidence,
            candidate_ids=_clean_refs(payload.get("candidate_ids"), limit=MAX_NODE_EVIDENCE_REFS),
            evidence_refs=_clean_refs(payload.get("evidence_refs"), limit=MAX_NODE_EVIDENCE_REFS),
            created_at=clip_signal_text(payload.get("created_at"), 80),
            updated_at=clip_signal_text(payload.get("updated_at"), 80),
            last_reinforced_at=clip_signal_text(payload.get("last_reinforced_at"), 80),
            last_decayed_at=clip_signal_text(payload.get("last_decayed_at"), 80),
            superseded_by=clip_signal_text(payload.get("superseded_by"), 120),
        )


@dataclass(frozen=True)
class GhostEdge:
    source: str
    target: str
    relation: str
    weight: float
    candidate_ids: tuple[str, ...]
    evidence_refs: tuple[str, ...]
    created_at: str
    updated_at: str
    last_reinforced_at: str
    last_decayed_at: str = ""

    def to_payload(self) -> dict[str, object]:
        return {
            "source": self.source,
            "target": self.target,
            "relation": self.relation,
            "weight": self.weight,
            "candidate_ids": list(self.candidate_ids),
            "evidence_refs": list(self.evidence_refs),
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "last_reinforced_at": self.last_reinforced_at,
            "last_decayed_at": self.last_decayed_at,
        }

    @classmethod
    def from_payload(cls, payload: object) -> GhostEdge | None:
        if not object_fields(payload) or not _valid_recorded_times(payload):
            return None
        source = clip_signal_text(payload.get("source"), 120)
        target = clip_signal_text(payload.get("target"), 120)
        relation = str(payload.get("relation") or "").strip().lower()
        if not source or not target or source == target or relation not in EDGE_RELATIONS:
            return None
        weight = _coerce_weight(payload.get("weight"))
        if weight is None:
            return None
        return cls(
            source=source,
            target=target,
            relation=relation,
            weight=weight,
            candidate_ids=_clean_refs(payload.get("candidate_ids"), limit=MAX_EDGE_EVIDENCE_REFS),
            evidence_refs=_clean_refs(payload.get("evidence_refs"), limit=MAX_EDGE_EVIDENCE_REFS),
            created_at=clip_signal_text(payload.get("created_at"), 80),
            updated_at=clip_signal_text(payload.get("updated_at"), 80),
            last_reinforced_at=clip_signal_text(payload.get("last_reinforced_at"), 80),
            last_decayed_at=clip_signal_text(payload.get("last_decayed_at"), 80),
        )


@dataclass(frozen=True)
class GhostReinforceResult:
    applied: bool
    reason: str
    node: GhostNode | None = None
    edges: tuple[GhostEdge, ...] = ()


class GhostHebbianStore:
    def __init__(self, state_home: str | Path = DEFAULT_STATE_HOME) -> None:
        self.database = GhostSQLite(state_home)
        self.graph = GhostGraph(self.database, "hebbian")
        self._maintenance_controls = GhostInboxStore(state_home)
        self.last_warnings: tuple[str, ...] = ()

    def reinforce_candidate(
        self,
        candidate: GhostMemoryCandidate,
        *,
        reward: float = 1.0,
        related_candidates: Iterable[GhostMemoryCandidate] = (),
    ) -> GhostReinforceResult:
        if candidate.status != "accepted":
            return GhostReinforceResult(False, "candidate_not_accepted")
        if not _candidate_has_provenance(candidate):
            return GhostReinforceResult(False, "missing_provenance")
        if not candidate.evidence_refs:
            return GhostReinforceResult(False, "missing_evidence_ref")
        try:
            with self.graph.transaction():
                nodes, edges = self._load_state_unlocked()
                node_by_id = {node.id: node for node in nodes}
                edge_by_key = {_edge_key(edge): edge for edge in edges}
                now = _common.now_iso_z()
                node_id = node_id_for_candidate(candidate)
                current = node_by_id.get(node_id)
                known_evidence = set(current.evidence_refs if current else ())
                new_evidence_refs = tuple(ref for ref in candidate.evidence_refs if ref not in known_evidence)
                if not new_evidence_refs:
                    changed_nodes: list[GhostNode] = []
                    if _candidate_is_manual_accept(candidate) and current is not None:
                        changed_nodes.extend(_supersede_conflicting_nodes(node_by_id, current, now=now))
                    changed_edges = (
                        _reinforce_coactivation_edges(
                            edge_by_key,
                            node_by_id,
                            current,
                            candidate,
                            related_candidates,
                            reward=reward,
                            now=now,
                        )
                        if current is not None
                        else list[GhostEdge]()
                    )
                    if not changed_nodes and not changed_edges:
                        return GhostReinforceResult(False, "duplicate_evidence")
                    events = [_node_event(node, action="superseded") for node in changed_nodes]
                    events.extend(_edge_event(edge, action="reinforced") for edge in changed_edges)
                    self._commit_reinforcement(node_by_id, edge_by_key, events, now=now)
                    reason = "backfilled_edges" if changed_edges else "superseded_conflicts"
                    return GhostReinforceResult(
                        True,
                        reason,
                        node=current,
                        edges=tuple(changed_edges),
                    )
                node = _reinforced_node(
                    current,
                    candidate,
                    node_id=node_id,
                    new_evidence_refs=new_evidence_refs,
                    reward=reward,
                    now=now,
                )
                node_by_id[node.id] = node
                changed_nodes = [node]
                if _candidate_is_manual_accept(candidate):
                    superseded = _supersede_conflicting_nodes(node_by_id, node, now=now)
                    changed_nodes.extend(superseded)
                changed_edges = _reinforce_coactivation_edges(
                    edge_by_key,
                    node_by_id,
                    node,
                    candidate,
                    related_candidates,
                    reward=reward,
                    now=now,
                )
                events = [
                    _node_event(
                        changed_node, action="superseded" if changed_node.status == "superseded" else "reinforced"
                    )
                    for changed_node in changed_nodes
                ]
                events.extend(_edge_event(edge, action="reinforced") for edge in changed_edges)
                self._commit_reinforcement(node_by_id, edge_by_key, events, now=now)
                return GhostReinforceResult(True, "reinforced", node=node, edges=tuple(changed_edges))
        except (cancellation.TaskCancelled, cancellation.DeadlineExceeded):
            raise
        except Exception:
            return GhostReinforceResult(False, "store_error")

    def _commit_reinforcement(
        self,
        node_by_id: Mapping[str, GhostNode],
        edge_by_key: Mapping[tuple[str, str, str], GhostEdge],
        events: Iterable[dict[str, object]],
        *,
        now: str,
        compact: bool = True,
    ) -> None:
        nodes = _bounded_nodes(node_by_id.values())
        edges = _bounded_edges(edge_by_key.values(), node_ids={node.id for node in nodes})
        removed_nodes = sorted(set(node_by_id) - {node.id for node in nodes})
        removed_edges = sorted(set(edge_by_key) - {_edge_key(edge) for edge in edges})
        transition_id = uuid.uuid4().hex
        self._append_events(
            dict(event, event_id=f"ghr_{transition_id}_{index}") for index, event in enumerate(events)
        )
        if removed_nodes or removed_edges:
            payload: dict[str, object] = {
                "node_ids": removed_nodes,
                "edge_keys": [list(key) for key in removed_edges],
            }
            self._append_events([_ghost_control_event(
                schema_version=HEBBIAN_SCHEMA_VERSION,
                event_name="ghost_hebbian_projection_pruned",
                event_id_prefix="ghp_",
                payload=payload,
                now=now,
            )])
        self._write_state(nodes, edges)
        if compact:
            self.compact_if_needed()

    def sync_from_inbox(self, inbox_store: GhostInboxStore) -> tuple[GhostReinforceResult, ...]:
        rows = inbox_store.list_candidates()
        accepted_rows = [row for row in rows if row.status == "accepted"]
        by_run: dict[str, list[GhostMemoryCandidate]] = {}
        for row in accepted_rows:
            by_run.setdefault(row.run_id or row.id, []).append(row)
        results: list[GhostReinforceResult] = []
        for row in rows:
            if row.status == "accepted":
                continue
            try:
                removed = self.remove_candidate(row)
            except (OSError, TypeError, ValueError):
                results.append(GhostReinforceResult(False, "remove_failed"))
                continue
            removed_map = removed if isinstance(removed, dict) else {}
            removed_count = _safe_count(removed_map.get("nodes", 0)) + _safe_count(removed_map.get("edges", 0))
            results.append(
                GhostReinforceResult(
                    removed_count > 0,
                    f"removed_{row.status}_candidate" if removed_count else f"no_{row.status}_state",
                )
            )
        for row in accepted_rows:
            related = [item for item in by_run.get(row.run_id or row.id, []) if item.id != row.id]
            results.append(self.reinforce_candidate(row, related_candidates=related))
        return tuple(results)

    def remove_candidate(self, candidate: GhostMemoryCandidate) -> dict[str, int]:
        with self.graph.transaction():
            nodes, edges = self._load_state_unlocked()
            candidate_node_id = node_id_for_candidate(candidate)
            removed_ids = {
                node.id for node in nodes if node.id == candidate_node_id or candidate.id in node.candidate_ids
            }
            if not removed_ids:
                return {"nodes": 0, "edges": 0}
            remaining_nodes = [node for node in nodes if node.id not in removed_ids]
            removed_edges = [edge for edge in edges if edge.source in removed_ids or edge.target in removed_ids]
            remaining_edges = [
                edge for edge in edges if edge.source not in removed_ids and edge.target not in removed_ids
            ]
            self._rewrite_events_from_state(
                remaining_nodes,
                remaining_edges,
                control_event=_ghost_control_event(
                    schema_version=HEBBIAN_SCHEMA_VERSION,
                    event_name="ghost_hebbian_candidate_removed",
                    payload={
                        "candidate_id": candidate.id,
                        "removed_nodes": len(removed_ids),
                        "removed_edges": len(removed_edges),
                    },
                    now=_common.now_iso_z(),
                ),
            )
            self._write_state(remaining_nodes, remaining_edges)
            return {"nodes": len(removed_ids), "edges": len(removed_edges)}

    def list_nodes(
        self,
        *,
        status: str | Iterable[str] | None = None,
        scope: str = "",
        project: str = "",
        session_id: str = "",
    ) -> tuple[GhostNode, ...]:
        with self.graph.transaction(write=False):
            return self._list_nodes_unlocked(
                status=status,
                scope=scope,
                project=project,
                session_id=session_id,
            )

    def _list_nodes_unlocked(
        self,
        *,
        status: str | Iterable[str] | None = None,
        scope: str = "",
        project: str = "",
        session_id: str = "",
    ) -> tuple[GhostNode, ...]:
        nodes, _edges = self._load_state_unlocked()
        statuses = _status_filter(status, allowed=NODE_STATUSES)
        normalized_scope = (scope or "").strip().lower()
        scope_ref = _scope_ref_for_filter(normalized_scope, project=project, session_id=session_id)
        rows: list[GhostNode] = []
        for node in nodes:
            if statuses and node.status not in statuses:
                continue
            if normalized_scope and node.scope != normalized_scope:
                continue
            if scope_ref and node.scope_ref != scope_ref:
                continue
            rows.append(node)

        def node_rank(node: GhostNode) -> tuple[float, str]:
            return node.weight, node.updated_at

        return tuple(sorted(rows, key=node_rank, reverse=True))

    def list_edges(self, *, relation: str = "") -> tuple[GhostEdge, ...]:
        with self.graph.transaction(write=False):
            return self._list_edges_unlocked(relation=relation)

    def _list_edges_unlocked(self, *, relation: str = "") -> tuple[GhostEdge, ...]:
        try:
            _nodes, edges = self._load_state_unlocked()
        except (cancellation.TaskCancelled, cancellation.DeadlineExceeded):
            raise
        except Exception:
            return ()
        normalized_relation = (relation or "").strip().lower()
        rows = [edge for edge in edges if not normalized_relation or edge.relation == normalized_relation]

        def edge_rank(edge: GhostEdge) -> tuple[float, str]:
            return edge.weight, edge.updated_at

        return tuple(sorted(rows, key=edge_rank, reverse=True))

    def export_state(self) -> dict[str, object]:
        with self.graph.transaction(write=False):
            nodes, edges = self._load_state_unlocked()
            return {
                "schema_version": HEBBIAN_SCHEMA_VERSION,
                "state": self._projection_payload(nodes, edges),
                "events": list(self._read_events_unlocked()),
                "warnings": list(self.last_warnings),
            }

    def reset_all(self) -> bool:
        self.graph.reset()
        self.last_warnings = ()
        return True

    def delete_scope(
        self,
        scope: str,
        *,
        project: str = "",
        session_id: str = "",
    ) -> dict[str, int]:
        normalized_scope = (scope or "").strip().lower()
        if normalized_scope not in SIGNAL_SCOPES:
            raise ValueError("scope must be user, project, or session")
        scope_ref = _scope_ref_for_filter(normalized_scope, project=project, session_id=session_id)
        if normalized_scope in {"project", "session"} and not scope_ref:
            raise ValueError(f"{normalized_scope} reference is required")
        with self.graph.transaction(deleting=True):
            nodes, edges = self._load_state_unlocked()
            removed_ids = {
                node.id
                for node in nodes
                if node.scope == normalized_scope and (normalized_scope == "user" or node.scope_ref == scope_ref)
            }
            if not removed_ids:
                return {"nodes": 0, "edges": 0}
            remaining_nodes = [node for node in nodes if node.id not in removed_ids]
            removed_edges = [edge for edge in edges if edge.source in removed_ids or edge.target in removed_ids]
            remaining_edges = [
                edge for edge in edges if edge.source not in removed_ids and edge.target not in removed_ids
            ]
            self._append_events([
                _ghost_control_event(
                    schema_version=HEBBIAN_SCHEMA_VERSION,
                    event_name="ghost_hebbian_scope_deleted",
                    event_id_prefix="ghd_",
                    payload={
                        "scope": normalized_scope,
                        "scope_ref": scope_ref,
                        "removed_nodes": len(removed_ids),
                        "removed_edges": len(removed_edges),
                    },
                    now=_common.now_iso_z(),
                ),
            ])
            self._write_state(remaining_nodes, remaining_edges)
            return {"nodes": len(removed_ids), "edges": len(removed_edges)}

    def rebuild_from_events(self) -> bool:
        try:
            with self.graph.transaction():
                nodes, edges = self._rebuild_state_from_events_unlocked()
                self._write_state(nodes, edges)
                return True
        except (cancellation.TaskCancelled, cancellation.DeadlineExceeded):
            raise
        except Exception:
            return False

    def decay(self, *, min_interval_seconds: int = 0, maintenance_as_of: str | None = None) -> dict[str, object]:
        time_input = _common.now_iso_z() if maintenance_as_of is None else maintenance_as_of
        now = parse_ts(time_input).isoformat().replace("+00:00", "Z")
        with self.graph.transaction():
            nodes, edges = self._load_state_unlocked()
            policy = self._maintenance_controls.maintenance_policy()
            denied_nodes = {node.id for node in nodes if not policy.allows_record(node.scope, node.scope_ref)}
            protected_edges = {_edge_key(edge) for edge in edges
                               if edge.source in denied_nodes or edge.target in denied_nodes}
            pinned_nodes = denied_nodes | {endpoint for edge in edges if _edge_key(edge) in protected_edges
                                           for endpoint in (edge.source, edge.target)}
            eligible_nodes = [node for node in nodes if node.id not in pinned_nodes]
            eligible_edges = [edge for edge in edges if _edge_key(edge) not in protected_edges]
            interval = max(0, coerce_int(min_interval_seconds or 0))
            if interval and not _any_decay_due((*eligible_nodes, *eligible_edges), now=now, min_interval_seconds=interval):
                return {
                    "removed_nodes": 0,
                    "removed_edges": 0,
                    "decayed_nodes": 0,
                    "decayed_edges": 0,
                    "skipped_reason": "min_interval",
                }
            decayed_nodes = [node if node.id in pinned_nodes else _decay_node(node, now=now) for node in nodes]
            decayed_edges = [edge if _edge_key(edge) in protected_edges else _decay_edge(edge, now=now) for edge in edges]
            bounded_nodes = _bounded_nodes(decayed_nodes)
            bounded_edges = _bounded_edges(decayed_edges, node_ids={node.id for node in bounded_nodes})
            removed_nodes = len(nodes) - len(bounded_nodes)
            removed_edges = len(edges) - len(bounded_edges)
            decayed_node_count = sum(
                1
                for before, after in zip(nodes, decayed_nodes, strict=False)
                if before.weight != after.weight or before.status != after.status
            )
            decayed_edge_count = sum(
                1 for before, after in zip(edges, decayed_edges, strict=False) if before.weight != after.weight
            )
            if not removed_nodes and not removed_edges and not decayed_node_count and not decayed_edge_count:
                return {
                    "removed_nodes": 0,
                    "removed_edges": 0,
                    "decayed_nodes": 0,
                    "decayed_edges": 0,
                    "skipped_reason": "no_change",
                }
            events = [_node_event(after, action="decayed", ts=now)
                      for before, after in zip(nodes, decayed_nodes, strict=True) if before != after]
            events.extend(_edge_event(after, action="decayed", ts=now)
                          for before, after in zip(edges, decayed_edges, strict=True) if before != after)
            events.append(_ghost_control_event(
                    schema_version=HEBBIAN_SCHEMA_VERSION,
                    event_name="ghost_hebbian_state_decayed",
                    payload={
                        "removed_nodes": removed_nodes,
                        "removed_edges": removed_edges,
                        "decayed_nodes": decayed_node_count,
                        "decayed_edges": decayed_edge_count,
                    },
                    now=now,
                ))
            self._commit_reinforcement(
                {node.id: node for node in decayed_nodes}, {_edge_key(edge): edge for edge in decayed_edges},
                events, now=now, compact=False,
            )
            return {
                "removed_nodes": removed_nodes,
                "removed_edges": removed_edges,
                "decayed_nodes": decayed_node_count,
                "decayed_edges": decayed_edge_count,
                "skipped_reason": "",
            }

    def compact_if_needed(self) -> dict[str, object]:
        with self.graph.transaction():
            before = self.graph.event_stats()
            if not over_compact_budget(before, max_events=MAX_HEBBIAN_EVENTS, max_bytes=MAX_HEBBIAN_EVENTS_BYTES):
                return _shared_compact_payload(True, False, before, before, ())
            if not self._maintenance_controls.maintenance_policy().allows_global_checkpoint:
                return _shared_compact_payload(True, False, before, before, ())
            nodes, edges = self._load_state_unlocked()
            self._rewrite_events_from_state(nodes, edges)
            self._write_state(nodes, edges)
            after = self.graph.event_stats()
            return _shared_compact_payload(True, True, before, after, ())

    def _load_state_unlocked(self) -> tuple[list[GhostNode], list[GhostEdge]]:
        raw_nodes, raw_edges = self.graph.rows()
        nodes = [GhostNode.from_payload(row) for row in raw_nodes]
        edges = [GhostEdge.from_payload(row) for row in raw_edges]
        if any(node is None for node in nodes) or any(edge is None for edge in edges):
            raise OSError("invalid Ghost Hebbian graph row")
        return [node for node in nodes if node is not None], [edge for edge in edges if edge is not None]

    def _rebuild_state_from_events_unlocked(self) -> tuple[list[GhostNode], list[GhostEdge]]:
        events = self._read_events_unlocked()
        nodes: dict[str, GhostNode] = {}
        edges: dict[tuple[str, str, str], GhostEdge] = {}
        for event in events:
            event_type = _replay_event_type(event)
            if event_type == "ghost_hebbian_scope_deleted":
                payload = _replay_control_payload(event_type, event.get("payload"))
                bounded_nodes = _bounded_nodes(nodes.values())
                bounded_edges = _bounded_edges(edges.values(), node_ids={node.id for node in bounded_nodes})
                if ({node.id for node in bounded_nodes} != set(nodes)
                        or {_edge_key(edge) for edge in bounded_edges} != set(edges)):
                    raise ValueError("Ghost Hebbian deletion follows an unrecorded pruning boundary")
                scope, scope_ref = payload["scope"], payload["scope_ref"]
                removed = {
                    node.id for node in nodes.values()
                    if node.scope == scope and (scope == "user" or node.scope_ref == scope_ref)
                }
                nodes = {identity: node for identity, node in nodes.items() if identity not in removed}
                edges = {
                    identity: edge for identity, edge in edges.items()
                    if edge.source not in removed and edge.target not in removed
                }
            elif event_type == "ghost_hebbian_projection_pruned":
                removed_nodes, removed_edges = _replay_pruned_payload(event.get("payload"))
                if not removed_nodes.issubset(nodes) or not removed_edges.issubset(edges):
                    raise ValueError("Ghost Hebbian pruning references absent rows")
                nodes = {identity: node for identity, node in nodes.items() if identity not in removed_nodes}
                edges = {
                    identity: edge for identity, edge in edges.items()
                    if identity not in removed_edges and edge.source not in removed_nodes and edge.target not in removed_nodes
                }
            elif event_type in {"ghost_hebbian_candidate_removed", "ghost_hebbian_state_decayed"}:
                # Their preceding snapshot or precise row events contain the mutation.
                # Validate the audit marker without applying it a second time.
                _replay_control_payload(event_type, event.get("payload"))
            elif event_type in {"ghost_hebbian_node_upsert", "ghost_hebbian_node_superseded"}:
                node = GhostNode.from_payload(event.get("node"))
                if node is None:
                    raise ValueError("invalid Ghost Hebbian replay node")
                if (event_type == "ghost_hebbian_node_superseded") != (node.status == "superseded"):
                    raise ValueError("Ghost Hebbian replay node status disagrees with event type")
                nodes[node.id] = node
            elif event_type == "ghost_hebbian_edge_upsert":
                edge = GhostEdge.from_payload(event.get("edge"))
                if edge is None:
                    raise ValueError("invalid Ghost Hebbian replay edge")
                edges[_edge_key(edge)] = edge
        bounded_nodes = _bounded_nodes(nodes.values())
        bounded_edges = _bounded_edges(edges.values(), node_ids={node.id for node in bounded_nodes})
        return bounded_nodes, bounded_edges

    def _read_events_unlocked(self) -> tuple[dict[str, object], ...]:
        return tuple(self.graph.events())

    def _write_state(self, nodes: Iterable[GhostNode], edges: Iterable[GhostEdge]) -> None:
        node_rows = _bounded_nodes(nodes)
        edge_rows = _bounded_edges(edges, node_ids={node.id for node in node_rows})
        self.graph.replace_rows((node.to_payload() for node in node_rows), (edge.to_payload() for edge in edge_rows))

    def _projection_payload(
        self,
        nodes: Iterable[GhostNode],
        edges: Iterable[GhostEdge],
    ) -> dict[str, object]:
        node_rows = _bounded_nodes(nodes)
        node_ids = {node.id for node in node_rows}
        edge_rows = _bounded_edges(edges, node_ids=node_ids)
        return {
            "schema_version": HEBBIAN_SCHEMA_VERSION,
            "kind": _STATE_KIND,
            "source": "ghost.sqlite3:hebbian",
            "updated_at": _common.now_iso_z(),
            "nodes": [node.to_payload() for node in node_rows],
            "edges": [edge.to_payload() for edge in edge_rows],
            "warnings": list(self.last_warnings),
        }

    def _append_events(self, events: Iterable[dict[str, object]]) -> None:
        self.graph.append(events)

    def _rewrite_events_from_state(
        self,
        nodes: Iterable[GhostNode],
        edges: Iterable[GhostEdge],
        *,
        control_event: dict[str, object] | None = None,
        ts: str | None = None,
    ) -> None:
        node_rows = _bounded_nodes(nodes)
        events = [_node_event(node, action="compacted", ts=ts) for node in node_rows]
        node_ids = {node.id for node in node_rows}
        events.extend(_edge_event(edge, action="compacted", ts=ts) for edge in _bounded_edges(edges, node_ids=node_ids))
        if control_event is not None:
            events.append(control_event)
        self._write_events_atomic(events)

    def _write_events_atomic(self, events: Iterable[dict[str, object]]) -> None:
        self.graph.append(events, replace=True)


def _valid_recorded_times(payload: dict[str, object]) -> bool:
    times: list[str] = []
    for field in ("created_at", "updated_at", "last_reinforced_at", "last_decayed_at"):
        value = payload.get(field, "")
        if not isinstance(value, str):
            return False
        if value:
            try:
                parse_ts(value)
            except ValueError:
                return False
        times.append(value)
    # Legacy rows may omit optional times, but never the entire decay anchor.
    return any(times[1:])


def _replay_pruned_payload(payload: object) -> tuple[set[str], set[tuple[str, str, str]]]:
    if not object_fields(payload) or set(payload) != {"node_ids", "edge_keys"}:
        raise ValueError("invalid Ghost Hebbian pruning payload")
    raw_nodes, raw_edges = payload["node_ids"], payload["edge_keys"]
    if not isinstance(raw_nodes, list) or not isinstance(raw_edges, list):
        raise ValueError("invalid Ghost Hebbian pruning arrays")
    node_values: Sequence[object] = raw_nodes
    edge_values: Sequence[object] = raw_edges
    nodes: set[str] = set()
    edges: set[tuple[str, str, str]] = set()
    for identity in node_values:
        if not isinstance(identity, str) or not identity.strip() or identity in nodes:
            raise ValueError("invalid Ghost Hebbian pruned node identity")
        nodes.add(identity)
    for value in edge_values:
        if not isinstance(value, list) or len(value) != 3:
            raise ValueError("invalid Ghost Hebbian pruned edge key")
        parts: Sequence[object] = value
        source, target, relation = parts
        if (not isinstance(source, str) or not source.strip() or not isinstance(target, str) or not target.strip()
                or not isinstance(relation, str) or relation not in EDGE_RELATIONS):
            raise ValueError("invalid Ghost Hebbian pruned edge identity")
        key = (source, target, relation)
        if key in edges:
            raise ValueError("duplicate Ghost Hebbian pruned edge")
        edges.add(key)
    if not nodes and not edges:
        raise ValueError("empty Ghost Hebbian pruning transition")
    return nodes, edges


def _replay_control_payload(event_type: str, payload: object) -> dict[str, object]:
    if not object_fields(payload):
        raise ValueError("invalid Ghost Hebbian control payload")
    count_keys = {"removed_nodes", "removed_edges"}
    if event_type == "ghost_hebbian_candidate_removed":
        expected_keys = count_keys | {"candidate_id"}
        candidate_id = payload.get("candidate_id")
        if not isinstance(candidate_id, str) or not candidate_id.strip():
            raise ValueError("invalid Ghost Hebbian removed candidate identity")
    elif event_type == "ghost_hebbian_scope_deleted":
        expected_keys = count_keys | {"scope", "scope_ref"}
        scope, scope_ref = payload.get("scope"), payload.get("scope_ref")
        if (not isinstance(scope, str) or scope not in SIGNAL_SCOPES or not isinstance(scope_ref, str)
                or (scope == "user" and scope_ref != "") or (scope != "user" and not scope_ref.strip())):
            raise ValueError("invalid Ghost Hebbian deletion scope")
    else:
        if event_type != "ghost_hebbian_state_decayed":
            raise ValueError("unknown Ghost Hebbian control event")
        # Both exact shapes were emitted with schema 1. A partial newer pair
        # is corruption, not the original two-counter historical format.
        if set(payload) != count_keys:
            count_keys |= {"decayed_nodes", "decayed_edges"}
        expected_keys = count_keys
    if set(payload) != expected_keys or not all(
        _common.valid_nonnegative_int_payload(payload.get(key)) for key in count_keys
    ):
        raise ValueError("invalid Ghost Hebbian control fields or counts")
    return payload


def _replay_event_type(event: dict[str, object]) -> str:
    version = event.get("schema_version")
    if type(version) is not int or version != HEBBIAN_SCHEMA_VERSION:
        raise ValueError("unsupported Ghost Hebbian replay schema")
    event_type = event.get("type")
    if not isinstance(event_type, str) or event_type not in _REPLAY_EVENT_TYPES:
        raise ValueError("unknown Ghost Hebbian replay event")
    parse_ts(event.get("ts"))
    return event_type


def node_id_for_candidate(candidate: GhostMemoryCandidate) -> str:
    raw = "|".join(
        (
            candidate.scope,
            _scope_ref_for_candidate(candidate),
            candidate.signal_kind,
            candidate.conflict_key,
            candidate.value_key,
        )
    )
    digest = hashlib.sha256(raw.encode("utf-8", "replace")).hexdigest()[:24]
    return f"ghn_{digest}"


def _reinforced_node(
    current: GhostNode | None,
    candidate: GhostMemoryCandidate,
    *,
    node_id: str,
    new_evidence_refs: tuple[str, ...],
    reward: float,
    now: str,
) -> GhostNode:
    confidence = _coerce_confidence(candidate.confidence) or 0.0
    if current is None:
        old_weight = 0.0
        created_at = now
        candidate_ids: tuple[str, ...] = ()
        evidence_refs: tuple[str, ...] = ()
    else:
        old_weight = _decayed_weight(current.weight, _decay_basis(current), now, NODE_HALF_LIFE_DAYS)
        created_at = current.created_at
        candidate_ids = current.candidate_ids
        evidence_refs = current.evidence_refs
    increment = NODE_LEARNING_RATE * _coerce_reward(reward) * confidence * max(1, len(new_evidence_refs))
    weight = _clamp01(old_weight + increment)
    return GhostNode(
        id=node_id,
        kind=candidate.signal_kind,
        label=clip_signal_text(candidate.summary),
        conflict_key=clip_signal_text(candidate.conflict_key, 180),
        value_key=clip_signal_text(candidate.value_key, 180),
        status="active",
        scope=candidate.scope,
        scope_ref=_scope_ref_for_candidate(candidate),
        weight=weight,
        confidence=max(confidence, current.confidence if current else 0.0),
        candidate_ids=_merge_refs(candidate_ids, (candidate.id,), limit=MAX_NODE_EVIDENCE_REFS),
        evidence_refs=_merge_refs(evidence_refs, new_evidence_refs, limit=MAX_NODE_EVIDENCE_REFS),
        created_at=created_at,
        updated_at=now,
        last_reinforced_at=now,
        last_decayed_at=now,
    )


def _supersede_conflicting_nodes(
    node_by_id: dict[str, GhostNode],
    accepted: GhostNode,
    *,
    now: str,
) -> list[GhostNode]:
    changed: list[GhostNode] = []
    for node_id, node in list(node_by_id.items()):
        if node_id == accepted.id:
            continue
        if node.status != "active":
            continue
        if node.scope != accepted.scope or node.scope_ref != accepted.scope_ref:
            continue
        if node.conflict_key != accepted.conflict_key:
            continue
        if node.value_key == accepted.value_key:
            continue
        superseded = replace(
            node,
            status="superseded",
            updated_at=now,
            superseded_by=accepted.id,
        )
        node_by_id[node_id] = superseded
        changed.append(superseded)
    return changed


def _reinforce_coactivation_edges(
    edge_by_key: dict[tuple[str, str, str], GhostEdge],
    node_by_id: dict[str, GhostNode],
    node: GhostNode,
    candidate: GhostMemoryCandidate,
    related_candidates: Iterable[GhostMemoryCandidate],
    *,
    reward: float,
    now: str,
) -> list[GhostEdge]:
    changed_edges: list[GhostEdge] = []
    for related in related_candidates:
        if not _can_coactivate(candidate, related):
            continue
        related_id = node_id_for_candidate(related)
        if related_id not in node_by_id:
            continue
        edge = _reinforced_edge(
            edge_by_key.get(_canonical_edge_key(node.id, related_id, "coactivated_with")),
            node.id,
            related_id,
            candidate,
            related,
            reward=reward,
            now=now,
        )
        if edge is not None:
            edge_by_key[_edge_key(edge)] = edge
            changed_edges.append(edge)
    return changed_edges


def _reinforced_edge(
    current: GhostEdge | None,
    source: str,
    target: str,
    candidate: GhostMemoryCandidate,
    related: GhostMemoryCandidate,
    *,
    reward: float,
    now: str,
) -> GhostEdge | None:
    if source == target:
        return None
    edge_source, edge_target, relation = _canonical_edge_key(source, target, "coactivated_with")
    edge_refs = (_coactivation_evidence_ref(candidate, related),)
    if current is not None and all(ref in current.evidence_refs for ref in edge_refs):
        return None
    confidence = min(_coerce_confidence(candidate.confidence) or 0.0, _coerce_confidence(related.confidence) or 0.0)
    if current is None:
        old_weight = 0.0
        created_at = now
        candidate_ids: tuple[str, ...] = ()
        evidence_refs: tuple[str, ...] = ()
    else:
        old_weight = _decayed_weight(current.weight, _decay_basis(current), now, EDGE_HALF_LIFE_DAYS)
        created_at = current.created_at
        candidate_ids = current.candidate_ids
        evidence_refs = current.evidence_refs
    increment = EDGE_LEARNING_RATE * _coerce_reward(reward) * confidence
    return GhostEdge(
        source=edge_source,
        target=edge_target,
        relation=relation,
        weight=_clamp01(old_weight + increment),
        candidate_ids=_merge_refs(candidate_ids, (candidate.id, related.id), limit=MAX_EDGE_EVIDENCE_REFS),
        evidence_refs=_merge_refs(evidence_refs, edge_refs, limit=MAX_EDGE_EVIDENCE_REFS),
        created_at=created_at,
        updated_at=now,
        last_reinforced_at=now,
        last_decayed_at=now,
    )


def _decay_node(node: GhostNode, *, now: str) -> GhostNode:
    decayed = _decayed_weight(node.weight, _decay_basis(node), now, NODE_HALF_LIFE_DAYS)
    status = "expired" if decayed < MIN_NODE_WEIGHT and node.status == "active" else node.status
    return replace(node, weight=decayed, status=status, updated_at=now, last_decayed_at=now)


def _decay_edge(edge: GhostEdge, *, now: str) -> GhostEdge:
    decayed = _decayed_weight(edge.weight, _decay_basis(edge), now, EDGE_HALF_LIFE_DAYS)
    return replace(edge, weight=decayed, updated_at=now, last_decayed_at=now)


def _decay_basis(row: GhostNode | GhostEdge) -> str:
    return _shared_decay_basis_of(row.last_decayed_at, row.last_reinforced_at, row.updated_at)


def _decayed_weight(weight: float, last_reinforced_at: str, now: str, half_life_days: float) -> float:
    return _clamp01(_shared_decayed_by_half_life(weight, last_reinforced_at, now, half_life_days))


def _any_decay_due(
    rows: Iterable[GhostNode | GhostEdge],
    *,
    now: str,
    min_interval_seconds: int,
) -> bool:
    return _shared_any_decay_due(
        (_decay_basis(row) for row in rows),
        now=now,
        min_interval_seconds=min_interval_seconds,
    )


def _bounded_nodes(nodes: Iterable[GhostNode]) -> list[GhostNode]:
    return _shared_bound_graph_nodes(nodes, min_active_weight=MIN_NODE_WEIGHT, limit=MAX_GHOST_NODES)


def _bounded_edges(edges: Iterable[GhostEdge], *, node_ids: set[str]) -> list[GhostEdge]:
    return _shared_bound_graph_edges(
        edges,
        node_ids=node_ids,
        min_weight=MIN_EDGE_WEIGHT,
        limit=MAX_GHOST_EDGES,
        max_out_degree=MAX_EDGE_OUT_DEGREE,
    )


def _node_event(node: GhostNode, *, action: str, ts: str | None = None) -> dict[str, object]:
    event_type = "ghost_hebbian_node_superseded" if node.status == "superseded" else "ghost_hebbian_node_upsert"
    return {
        "schema_version": HEBBIAN_SCHEMA_VERSION,
        "type": event_type,
        "ts": _common.now_iso_z() if ts is None else ts,
        "action": action,
        "node": node.to_payload(),
    }


def _edge_event(edge: GhostEdge, *, action: str, ts: str | None = None) -> dict[str, object]:
    return {
        "schema_version": HEBBIAN_SCHEMA_VERSION,
        "type": "ghost_hebbian_edge_upsert",
        "ts": _common.now_iso_z() if ts is None else ts,
        "action": action,
        "edge": edge.to_payload(),
    }


def _candidate_has_provenance(candidate: GhostMemoryCandidate) -> bool:
    if not candidate.scope or candidate.scope not in SIGNAL_SCOPES:
        return False
    if not candidate.session_id and not candidate.run_id:
        return False
    if candidate.scope == "project" and not candidate.project:
        return False
    return not (candidate.scope == "session" and not candidate.session_id)


def _candidate_is_manual_accept(candidate: GhostMemoryCandidate) -> bool:
    return bool(candidate.reviewed_at and candidate.gate_reason == "manual_accept")


def _can_coactivate(candidate: GhostMemoryCandidate, related: GhostMemoryCandidate) -> bool:
    return (
        related.status == "accepted"
        and bool(candidate.run_id)
        and candidate.run_id == related.run_id
        and _candidate_has_provenance(related)
    )


def _coactivation_evidence_ref(
    candidate: GhostMemoryCandidate,
    related: GhostMemoryCandidate,
) -> str:
    left, right = sorted((candidate.id, related.id))
    run_id = clip_signal_text(candidate.run_id, 80)
    return clip_signal_text(f"co:{run_id}:{left}:{right}", 160)


def _scope_ref_for_candidate(candidate: GhostMemoryCandidate) -> str:
    return candidate.scope_ref


def _scope_ref_for_filter(scope: str, *, project: str, session_id: str) -> str:
    if scope == "project":
        return _common.normalize_project(project)
    if scope == "session":
        return clip_signal_text(session_id, 120)
    return ""


def _edge_key(edge: GhostEdge) -> tuple[str, str, str]:
    return _canonical_edge_key(edge.source, edge.target, edge.relation)


def _canonical_edge_key(source: str, target: str, relation: str) -> tuple[str, str, str]:
    a, b = sorted((source, target))
    return a, b, (relation or "coactivated_with")


def _merge_refs(
    current: Iterable[str],
    incoming: Iterable[str],
    *,
    limit: int,
) -> tuple[str, ...]:
    out: list[str] = []
    for value in (*tuple(current or ()), *tuple(incoming or ())):
        ref = clip_signal_text(value, 160)
        if ref and ref not in out:
            out.append(ref)
    return tuple(out[-max(1, coerce_int(limit or 1, default=1)) :])


def _clean_refs(value: object, *, limit: int) -> tuple[str, ...]:
    if not isinstance(value, (list, tuple)):
        return ()
    items: Iterable[object] = value
    return _merge_refs((), (str(item) for item in items), limit=limit)


def _status_filter(value: str | Iterable[str] | None, *, allowed: tuple[str, ...]) -> set[str]:
    if value is None:
        return set()
    raw_values = value.split(",") if isinstance(value, str) else list(value)
    return {item.strip().lower() for item in raw_values if item.strip().lower() in allowed}


def _coerce_weight(value: object) -> float | None:
    return coerce_unit_float(value, digits=6)


def _coerce_confidence(value: object) -> float | None:
    return coerce_unit_float(value, digits=4)


def _coerce_reward(value: object) -> float:
    reward = coerce_float(value, default=1.0)
    if not math.isfinite(reward):
        return 1.0
    return max(0.0, min(1.0, reward))


def _clamp01(value: float) -> float:
    try:
        number = float(value or 0.0)
    except (TypeError, ValueError, OverflowError):
        return 0.0
    if not math.isfinite(number):
        return 0.0
    return round(max(0.0, min(1.0, number)), 6)
