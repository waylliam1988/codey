"""Bounded local Affinity Index for audited Ghost facts.

Affinity is a deterministic association ledger. It is not evidence, not a
permission system, and not an execution policy.
"""

from __future__ import annotations

import ast
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
import hashlib
import json
import math
import os
from pathlib import Path
from typing import Any, Iterable, Mapping
import uuid

from codey.ghost.schema import clip_signal_text, contains_sensitive_signal_text
from codey.local_store import DEFAULT_STATE_HOME, delete_file, project_key, read_json, session_key, write_json_atomic


AFFINITY_SCHEMA_VERSION = 1
MAX_AFFINITY_NODES = 500
MAX_AFFINITY_EDGES = 2_000
MAX_AFFINITY_EVENTS = 5_000
MAX_AFFINITY_STATE_BYTES = 1024 * 1024
MAX_AFFINITY_EVENTS_BYTES = 1024 * 1024
MAX_AFFINITY_REFS = 32
MAX_AFFINITY_REF_HASHES = 512
MAX_AFFINITY_HINT_REFS = 8
MAX_AFFINITY_WARNINGS = 20
MAX_EDGE_OUT_DEGREE = 16
NODE_LEARNING_RATE = 0.22
EDGE_LEARNING_RATE = 0.16
NODE_HALF_LIFE_DAYS = 90.0
EDGE_HALF_LIFE_DAYS = 120.0
MIN_NODE_WEIGHT = 0.04
MIN_EDGE_WEIGHT = 0.01
MAX_HINTS = 16
_STATE_KIND = "ghost_affinity_state_projection"

AFFINITY_SCOPES = frozenset({"user", "project", "session"})
AFFINITY_NODE_KINDS = frozenset({
    "user_preference",
    "project",
    "research_concept",
    "correction",
    "action_tendency",
    "provider_behavior",
    "task_type",
})
AFFINITY_NODE_STATUSES = frozenset({"active", "expired", "superseded"})
AFFINITY_EDGE_RELATIONS = frozenset({
    "associated_with",
    "prefers_for",
    "works_well_for",
    "struggles_with",
    "mentions_concept",
    "used_in_task",
})
AFFINITY_EDGE_STATUSES = frozenset({"active", "expired"})
HINT_KINDS = frozenset({
    "directive_order",
    "work_priority",
    "research_priority",
})

_HEBBIAN_KIND_MAP = {
    "style_preference": "user_preference",
    "correction": "correction",
    "action_tendency": "action_tendency",
    "research_interest": "research_concept",
}
_WORK_STATUS_REWARD = {
    "queued": 0.45,
    "running": 0.5,
    "done": 0.9,
    "blocked": 0.35,
}
_PROVIDER_ERROR_KINDS = frozenset({
    "timeout",
    "parse_error",
    "tool_protocol_error",
    "transient",
    "rate_limited",
    "control_missing",
    "submission_uncertain",
    "response_missing",
    "readiness_stale",
    "authentication_required",
    "challenge_required",
    "transient_send_failed",
})


@dataclass(frozen=True)
class AffinityNode:
    id: str
    kind: str
    key: str
    label: str
    scope: str
    scope_ref: str
    status: str
    weight: float
    confidence: float
    source_refs: tuple[str, ...] = ()
    evidence_refs: tuple[str, ...] = ()
    source_ref_hashes: tuple[str, ...] = ()
    evidence_ref_hashes: tuple[str, ...] = ()
    metadata: Mapping[str, object] = field(default_factory=dict)
    created_at: str = ""
    updated_at: str = ""
    last_reinforced_at: str = ""
    last_decayed_at: str = ""

    def to_payload(self) -> dict[str, object]:
        return {
            "id": self.id,
            "kind": self.kind,
            "key": self.key,
            "label": self.label,
            "scope": self.scope,
            "scope_ref": self.scope_ref,
            "status": self.status,
            "weight": self.weight,
            "confidence": self.confidence,
            "source_refs": list(self.source_refs),
            "evidence_refs": list(self.evidence_refs),
            "source_ref_hashes": list(_bounded_ref_hashes(self.source_ref_hashes or self.source_refs)),
            "evidence_ref_hashes": list(_bounded_ref_hashes(self.evidence_ref_hashes or self.evidence_refs)),
            "metadata": _clean_metadata(self.metadata),
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "last_reinforced_at": self.last_reinforced_at,
            "last_decayed_at": self.last_decayed_at,
        }

    @classmethod
    def from_payload(cls, payload: object) -> "AffinityNode | None":
        if not isinstance(payload, Mapping):
            return None
        kind = _clean_node_kind(payload.get("kind"))
        scope = _clean_scope(payload.get("scope"))
        status = _clean_node_status(payload.get("status"))
        if not kind or not scope or not status:
            return None
        node_id = clip_signal_text(payload.get("id"), 120)
        key = _clean_key(payload.get("key"), 180)
        label = _clean_label(payload.get("label"), 180)
        if not node_id or not key or not label:
            return None
        weight = _unit_float_or_none(payload.get("weight"))
        confidence = _unit_float_or_none(payload.get("confidence"))
        if weight is None or confidence is None:
            return None
        return cls(
            id=node_id,
            kind=kind,
            key=key,
            label=label,
            scope=scope,
            scope_ref=clip_signal_text(payload.get("scope_ref"), 120),
            status=status,
            weight=weight,
            confidence=confidence,
            source_refs=_bounded_refs(payload.get("source_refs")),
            evidence_refs=_bounded_refs(payload.get("evidence_refs")),
            source_ref_hashes=_bounded_ref_hashes(payload.get("source_ref_hashes") or payload.get("source_refs")),
            evidence_ref_hashes=_bounded_ref_hashes(payload.get("evidence_ref_hashes") or payload.get("evidence_refs")),
            metadata=_clean_metadata(payload.get("metadata")),
            created_at=clip_signal_text(payload.get("created_at"), 80),
            updated_at=clip_signal_text(payload.get("updated_at"), 80),
            last_reinforced_at=clip_signal_text(payload.get("last_reinforced_at"), 80),
            last_decayed_at=clip_signal_text(payload.get("last_decayed_at"), 80),
        )


@dataclass(frozen=True)
class AffinityEdge:
    id: str
    source: str
    target: str
    relation: str
    scope: str
    scope_ref: str
    status: str
    weight: float
    confidence: float
    source_refs: tuple[str, ...] = ()
    proof_refs: tuple[str, ...] = ()
    source_ref_hashes: tuple[str, ...] = ()
    proof_ref_hashes: tuple[str, ...] = ()
    created_at: str = ""
    updated_at: str = ""
    last_reinforced_at: str = ""
    last_decayed_at: str = ""

    def to_payload(self) -> dict[str, object]:
        return {
            "id": self.id,
            "source": self.source,
            "target": self.target,
            "relation": self.relation,
            "scope": self.scope,
            "scope_ref": self.scope_ref,
            "status": self.status,
            "weight": self.weight,
            "confidence": self.confidence,
            "source_refs": list(self.source_refs),
            "proof_refs": list(self.proof_refs),
            "source_ref_hashes": list(_bounded_ref_hashes(self.source_ref_hashes or self.source_refs)),
            "proof_ref_hashes": list(_bounded_ref_hashes(self.proof_ref_hashes or self.proof_refs)),
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "last_reinforced_at": self.last_reinforced_at,
            "last_decayed_at": self.last_decayed_at,
        }

    @classmethod
    def from_payload(cls, payload: object) -> "AffinityEdge | None":
        if not isinstance(payload, Mapping):
            return None
        relation = _clean_relation(payload.get("relation"))
        scope = _clean_scope(payload.get("scope"))
        status = _clean_edge_status(payload.get("status"))
        if not relation or not scope or not status:
            return None
        edge_id = clip_signal_text(payload.get("id"), 120)
        source = clip_signal_text(payload.get("source"), 120)
        target = clip_signal_text(payload.get("target"), 120)
        if not edge_id or not source or not target or source == target:
            return None
        weight = _unit_float_or_none(payload.get("weight"))
        confidence = _unit_float_or_none(payload.get("confidence"))
        if weight is None or confidence is None:
            return None
        return cls(
            id=edge_id,
            source=source,
            target=target,
            relation=relation,
            scope=scope,
            scope_ref=clip_signal_text(payload.get("scope_ref"), 120),
            status=status,
            weight=weight,
            confidence=confidence,
            source_refs=_bounded_refs(payload.get("source_refs")),
            proof_refs=_bounded_refs(payload.get("proof_refs")),
            source_ref_hashes=_bounded_ref_hashes(payload.get("source_ref_hashes") or payload.get("source_refs")),
            proof_ref_hashes=_bounded_ref_hashes(payload.get("proof_ref_hashes") or payload.get("proof_refs")),
            created_at=clip_signal_text(payload.get("created_at"), 80),
            updated_at=clip_signal_text(payload.get("updated_at"), 80),
            last_reinforced_at=clip_signal_text(payload.get("last_reinforced_at"), 80),
            last_decayed_at=clip_signal_text(payload.get("last_decayed_at"), 80),
        )


@dataclass(frozen=True)
class AffinityHint:
    kind: str
    target: str
    confidence: float
    weight: float
    reason_code: str
    source_refs: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()

    def to_payload(self) -> dict[str, object]:
        return {
            "kind": self.kind,
            "target": self.target,
            "confidence": self.confidence,
            "weight": self.weight,
            "reason_code": self.reason_code,
            "source_refs": list(self.source_refs),
            "warnings": list(self.warnings),
        }


@dataclass(frozen=True)
class GhostAffinitySyncResult:
    ok: bool
    skipped_reason: str = ""
    nodes_changed: int = 0
    edges_changed: int = 0
    total_nodes: int = 0
    total_edges: int = 0
    warnings: tuple[str, ...] = ()


@dataclass(frozen=True)
class _NodeSpec:
    kind: str
    key: str
    label: str
    scope: str
    scope_ref: str
    confidence: float
    reward: float
    source_refs: tuple[str, ...]
    evidence_refs: tuple[str, ...] = ()
    metadata: Mapping[str, object] = field(default_factory=dict)


@dataclass(frozen=True)
class _EdgeSpec:
    source: str
    target: str
    relation: str
    scope: str
    scope_ref: str
    confidence: float
    reward: float
    source_refs: tuple[str, ...]
    proof_refs: tuple[str, ...] = ()


class GhostAffinityStore:
    def __init__(self, state_home: str | Path = DEFAULT_STATE_HOME) -> None:
        self.directory = Path(state_home) / "ghost"
        self.projection_path = self.directory / "affinity.json"
        self.events_path = self.directory / "affinity_events.jsonl"
        self.last_warnings: tuple[str, ...] = ()
        self._events_read_blocked = False
        self._events_blocked_reason = ""

    def sync_from_sources(
        self,
        *,
        hebbian_store: Any = None,
        work_queue_store: Any = None,
        research_interest_candidates: Iterable[Any] = (),
        router_store: Any = None,
        run_projection: Any = None,
        terminal_event: Mapping[str, object] | None = None,
        session_id: str = "",
        project: str = "",
    ) -> GhostAffinitySyncResult:
        try:
            nodes, edges = self._load_state_for_mutation()
            if self._events_read_blocked:
                return self._sync_failed(self._events_blocked_reason or "events_read_blocked")
            node_by_id = {node.id: node for node in nodes}
            edge_by_id = {edge.id: edge for edge in edges}
            now = _now()
            node_specs, edge_specs = self._source_specs(
                hebbian_store=hebbian_store,
                work_queue_store=work_queue_store,
                research_interest_candidates=research_interest_candidates,
                router_store=router_store,
                run_projection=run_projection,
                terminal_event=terminal_event,
                session_id=session_id,
                project=project,
            )
            changed_nodes: list[AffinityNode] = []
            for spec in node_specs:
                node, changed = _reinforce_node(node_by_id.get(_node_id(spec.kind, spec.scope, spec.scope_ref, spec.key)), spec, now=now)
                if changed:
                    node_by_id[node.id] = node
                    changed_nodes.append(node)
            changed_edges: list[AffinityEdge] = []
            for spec in edge_specs:
                if spec.source not in node_by_id or spec.target not in node_by_id:
                    continue
                edge, changed = _reinforce_edge(edge_by_id.get(_edge_id(spec.source, spec.target, spec.relation, spec.scope, spec.scope_ref)), spec, now=now)
                if changed:
                    edge_by_id[edge.id] = edge
                    changed_edges.append(edge)
            bounded_nodes = _bounded_nodes(node_by_id.values())
            bounded_node_ids = {node.id for node in bounded_nodes}
            bounded_edges = _bounded_edges(edge_by_id.values(), node_ids=bounded_node_ids)
            bounded_edge_ids = {row.id for row in bounded_edges}
            if not changed_nodes and not changed_edges:
                self.last_warnings = ()
                return GhostAffinitySyncResult(
                    True,
                    skipped_reason="no_change",
                    total_nodes=len(bounded_nodes),
                    total_edges=len(bounded_edges),
                )
            events = [
                _node_event(node, action="reinforced")
                for node in changed_nodes
                if node.id in bounded_node_ids
            ]
            events.extend(
                _edge_event(edge, action="reinforced")
                for edge in changed_edges
                if edge.id in bounded_edge_ids
            )
            if not self._append_events_atomic(events):
                return self._sync_failed("event_write_failed")
            self._write_projection_best_effort(bounded_nodes, bounded_edges)
            self._compact_if_needed(bounded_nodes, bounded_edges)
            return GhostAffinitySyncResult(
                True,
                nodes_changed=len(changed_nodes),
                edges_changed=len(changed_edges),
                total_nodes=len(bounded_nodes),
                total_edges=len(bounded_edges),
                warnings=self.last_warnings,
            )
        except (OSError, TypeError, ValueError):
            return self._sync_failed("affinity_error")

    def list_nodes(
        self,
        *,
        kind: str = "",
        status: str = "",
        scope: str = "",
        project: str = "",
        session_id: str = "",
    ) -> tuple[AffinityNode, ...]:
        try:
            nodes, _edges = self._load_state_for_read()
        except Exception:
            return ()
        kinds = _filter_values(kind, AFFINITY_NODE_KINDS)
        statuses = _filter_values(status, AFFINITY_NODE_STATUSES)
        normalized_scope = _clean_scope(scope)
        rows = []
        for node in nodes:
            if kinds and node.kind not in kinds:
                continue
            if statuses and node.status not in statuses:
                continue
            if not _scope_visible_for_filter(
                node.scope,
                node.scope_ref,
                scope=normalized_scope,
                project=project,
                session_id=session_id,
            ):
                continue
            rows.append(node)
        return tuple(sorted(rows, key=lambda item: (item.status == "active", item.weight, item.updated_at), reverse=True))

    def list_edges(
        self,
        *,
        relation: str = "",
        status: str = "",
        scope: str = "",
        project: str = "",
        session_id: str = "",
    ) -> tuple[AffinityEdge, ...]:
        try:
            _nodes, edges = self._load_state_for_read()
        except Exception:
            return ()
        relations = _filter_values(relation, AFFINITY_EDGE_RELATIONS)
        statuses = _filter_values(status, AFFINITY_EDGE_STATUSES)
        normalized_scope = _clean_scope(scope)
        rows = []
        for edge in edges:
            if relations and edge.relation not in relations:
                continue
            if statuses and edge.status not in statuses:
                continue
            if not _scope_visible_for_filter(
                edge.scope,
                edge.scope_ref,
                scope=normalized_scope,
                project=project,
                session_id=session_id,
            ):
                continue
            rows.append(edge)
        return tuple(sorted(rows, key=lambda item: (item.status == "active", item.weight, item.updated_at), reverse=True))

    def query_hints(
        self,
        kind: str,
        targets: Iterable[Any],
        *,
        project: str = "",
        session_id: str = "",
    ) -> tuple[AffinityHint, ...]:
        clean_kind = _clean_hint_kind(kind)
        if clean_kind == "directive_order":
            return self.query_directive_order_hints(targets, project=project, session_id=session_id)
        if clean_kind == "work_priority":
            return self.query_work_priority_hints(targets, project=project, session_id=session_id)
        if clean_kind == "research_priority":
            return self.query_research_priority_hints(targets, project=project, session_id=session_id)
        return ()

    def query_directive_order_hints(
        self,
        nodes: Iterable[Any],
        *,
        project: str = "",
        session_id: str = "",
    ) -> tuple[AffinityHint, ...]:
        wanted = {
            clip_signal_text(getattr(node, "id", ""), 120)
            for node in nodes
            if clip_signal_text(getattr(node, "id", ""), 120)
        }
        if not wanted:
            return ()
        affinity_nodes, _edges = self._load_state_for_hint()
        hints: list[AffinityHint] = []
        for node in affinity_nodes:
            if node.status != "active":
                continue
            if not _scope_visible_for_filter(
                node.scope,
                node.scope_ref,
                project=project,
                session_id=session_id,
            ):
                continue
            hebbian_id = clip_signal_text(dict(node.metadata).get("hebbian_node_id"), 120)
            if not hebbian_id or hebbian_id not in wanted:
                continue
            hints.append(_hint(
                "directive_order",
                hebbian_id,
                node.weight,
                node.confidence,
                "confirmed_memory_reinforced",
                node.source_refs,
            ))
        return _bounded_hints(hints)

    def query_work_priority_hints(
        self,
        items: Iterable[Any],
        *,
        project: str = "",
        session_id: str = "",
    ) -> tuple[AffinityHint, ...]:
        nodes, edges = self._load_state_for_hint()
        active_nodes = {node.id: node for node in nodes if node.status == "active"}
        active_edges_by_target: dict[str, list[AffinityEdge]] = {}
        for edge in edges:
            if edge.status != "active":
                continue
            if not _scope_matches_values(edge.scope, edge.scope_ref, project=project, session_id=session_id):
                continue
            active_edges_by_target.setdefault(edge.target, []).append(edge)
        hints: list[AffinityHint] = []
        for item in list(items or []):
            item_id = clip_signal_text(_field(item, "id"), 120)
            if not item_id:
                continue
            scope, scope_ref = _scope_from_source(item, fallback_session_id=session_id, fallback_project=project)
            task_key = _clean_key(_field(item, "kind"), 180)
            task_id = _node_id("task_type", scope, scope_ref, task_key) if task_key else ""
            weight = 0.0
            confidence = 0.0
            refs: list[str] = []
            reason = ""
            task_node = active_nodes.get(task_id)
            if task_node is not None:
                weight = max(weight, task_node.weight)
                confidence = max(confidence, task_node.confidence)
                refs.extend(task_node.source_refs)
                reason = "task_type_reinforced"
            for concept in _concepts_from_work_item(item):
                concept_id = _node_id("research_concept", scope, scope_ref, concept)
                concept_node = active_nodes.get(concept_id)
                if concept_node is None:
                    continue
                weighted = concept_node.weight * 0.8
                if weighted > weight:
                    reason = "research_concept_reinforced"
                weight = max(weight, weighted)
                confidence = max(confidence, concept_node.confidence)
                refs.extend(concept_node.source_refs)
            for edge in active_edges_by_target.get(task_id, ()):
                weighted = edge.weight * 0.6
                if weighted > weight:
                    reason = "associated_task_reinforced"
                weight = max(weight, weighted)
                confidence = max(confidence, edge.confidence)
                refs.extend(edge.source_refs)
            if weight > 0.0:
                hints.append(_hint(
                    "work_priority",
                    item_id,
                    weight,
                    confidence or 0.5,
                    reason or "affinity_reinforced",
                    refs,
                ))
        return _bounded_hints(hints)

    def query_research_priority_hints(
        self,
        candidates: Iterable[Any],
        *,
        project: str = "",
        session_id: str = "",
    ) -> tuple[AffinityHint, ...]:
        nodes, _edges = self._load_state_for_hint()
        active_nodes = {node.id: node for node in nodes if node.status == "active"}
        hints: list[AffinityHint] = []
        for candidate in list(candidates or []):
            candidate_id = clip_signal_text(_field(candidate, "id"), 120)
            if not candidate_id:
                continue
            scope, scope_ref = _scope_from_source(candidate, fallback_session_id=session_id, fallback_project=project)
            best_weight = 0.0
            best_confidence = 0.0
            refs: list[str] = []
            for concept in _concepts_from_candidate(candidate):
                node = active_nodes.get(_node_id("research_concept", scope, scope_ref, concept))
                if node is None:
                    continue
                best_weight = max(best_weight, node.weight)
                best_confidence = max(best_confidence, node.confidence)
                refs.extend(node.source_refs)
            if best_weight > 0.0:
                hints.append(_hint(
                    "research_priority",
                    candidate_id,
                    best_weight,
                    best_confidence or 0.5,
                    "research_concept_reinforced",
                    refs,
                ))
        return _bounded_hints(hints)

    def export_state(self) -> dict[str, object]:
        events = self._read_events()
        orphan_projection = not self.events_path.exists() and self.projection_path.exists()
        event_warnings = _bounded_warnings((
            *self.last_warnings,
            *(("affinity_events_missing",) if orphan_projection else ()),
        ))
        if self._events_read_blocked or orphan_projection:
            nodes, edges = self._load_projection_rows()
        else:
            nodes, edges = _rows_from_events(events)
        projection = _projection_payload(nodes, edges, generated_at=_now(), warnings=event_warnings)
        if orphan_projection:
            projection["diagnostic"] = {
                "projection_only": True,
                "source_events_missing": True,
            }
        return {
            "schema_version": AFFINITY_SCHEMA_VERSION,
            "affinity": projection,
            "affinity_events": events,
            "warnings": list(event_warnings),
        }

    def reset_all(self) -> bool:
        try:
            delete_file(self.projection_path)
            delete_file(self.events_path)
            return True
        except OSError:
            return False

    def delete_scope(
        self,
        scope: str,
        *,
        project: str = "",
        session_id: str = "",
    ) -> dict[str, object]:
        normalized_scope = _clean_scope(scope)
        if not normalized_scope:
            raise ValueError("scope must be user, project, or session")
        scope_ref = _scope_ref_for_filter(normalized_scope, project=project, session_id=session_id)
        if normalized_scope in {"project", "session"} and not scope_ref:
            raise ValueError(f"{normalized_scope} reference is required")
        if not self.events_path.exists() and self.projection_path.exists():
            nodes, edges = self._load_projection_rows()
            return self._delete_scope_from_rows(
                nodes,
                edges,
                normalized_scope=normalized_scope,
                scope_ref=scope_ref,
                projection_only=True,
            )
        nodes, edges = self._load_state_for_mutation()
        if self._events_read_blocked:
            raise OSError("ghost affinity events are unreadable")
        return self._delete_scope_from_rows(
            nodes,
            edges,
            normalized_scope=normalized_scope,
            scope_ref=scope_ref,
            projection_only=False,
        )

    def _delete_scope_from_rows(
        self,
        nodes: Iterable[AffinityNode],
        edges: Iterable[AffinityEdge],
        *,
        normalized_scope: str,
        scope_ref: str,
        projection_only: bool,
    ) -> dict[str, object]:
        node_rows = list(nodes)
        edge_rows = list(edges)
        removed_node_ids = {
            node.id for node in node_rows
            if node.scope == normalized_scope and (normalized_scope == "user" or node.scope_ref == scope_ref)
        }
        kept_nodes = [node for node in node_rows if node.id not in removed_node_ids]
        kept_node_ids = {node.id for node in kept_nodes}
        kept_edges = [
            edge for edge in edge_rows
            if edge.source in kept_node_ids
            and edge.target in kept_node_ids
            and not (edge.scope == normalized_scope and (normalized_scope == "user" or edge.scope_ref == scope_ref))
        ]
        removed_edges = len(edge_rows) - len(kept_edges)
        warnings = (
            ("affinity_events_missing", "affinity_projection_only_delete")
            if projection_only else ()
        )
        if not removed_node_ids and not removed_edges:
            return {"nodes": 0, "edges": 0, "warnings": list(warnings)}
        if projection_only:
            if kept_nodes or kept_edges:
                self._write_projection(kept_nodes, kept_edges, warnings=warnings)
            else:
                delete_file(self.projection_path)
            self.last_warnings = _bounded_warnings(warnings)
            return {"nodes": len(removed_node_ids), "edges": removed_edges, "warnings": list(warnings)}
        self._rewrite_events_from_state(
            kept_nodes,
            kept_edges,
            control_event=_control_event(
                "ghost_affinity_scope_deleted",
                {
                    "scope": normalized_scope,
                    "scope_ref": scope_ref if normalized_scope != "user" else "",
                    "removed_nodes": len(removed_node_ids),
                    "removed_edges": removed_edges,
                },
            ),
        )
        self._write_projection(kept_nodes, kept_edges, warnings=[])
        return {"nodes": len(removed_node_ids), "edges": removed_edges, "warnings": []}

    def rebuild_from_events(self) -> bool:
        try:
            events = self._read_events()
            if self._events_read_blocked:
                return False
            nodes, edges = _rows_from_events(events)
            self._write_projection(nodes, edges, warnings=self.last_warnings)
            return True
        except (OSError, TypeError, ValueError):
            return False

    def decay(self, *, min_interval_seconds: int = 0) -> dict[str, object]:
        if self.events_path.exists():
            self._read_events()
            if self._events_read_blocked:
                return {
                    "removed_nodes": 0,
                    "removed_edges": 0,
                    "decayed_nodes": 0,
                    "decayed_edges": 0,
                    "skipped_reason": self._events_blocked_reason or "events_read_blocked",
                    "warnings": list(self.last_warnings),
                }
        nodes, edges = self._load_state_for_mutation()
        if self._events_read_blocked:
            return {
                "removed_nodes": 0,
                "removed_edges": 0,
                "decayed_nodes": 0,
                "decayed_edges": 0,
                "skipped_reason": self._events_blocked_reason or "events_read_blocked",
                "warnings": list(self.last_warnings),
            }
        now = _now()
        interval = max(0, int(min_interval_seconds or 0))
        if interval and not _any_decay_due((*nodes, *edges), now=now, min_interval_seconds=interval):
            return {
                "removed_nodes": 0,
                "removed_edges": 0,
                "decayed_nodes": 0,
                "decayed_edges": 0,
                "skipped_reason": "min_interval",
            }
        decayed_nodes = [_decay_node(node, now=now) for node in nodes]
        decayed_edges = [_decay_edge(edge, now=now) for edge in edges]
        bounded_nodes = _bounded_nodes(decayed_nodes)
        bounded_edges = _bounded_edges(decayed_edges, node_ids={node.id for node in bounded_nodes})
        decayed_node_count = sum(
            1 for before, after in zip(nodes, decayed_nodes, strict=False)
            if before.weight != after.weight or before.status != after.status
        )
        decayed_edge_count = sum(
            1 for before, after in zip(edges, decayed_edges, strict=False)
            if before.weight != after.weight or before.status != after.status
        )
        removed_nodes = len(nodes) - len(bounded_nodes)
        removed_edges = len(edges) - len(bounded_edges)
        if not removed_nodes and not removed_edges and not decayed_node_count and not decayed_edge_count:
            return {
                "removed_nodes": 0,
                "removed_edges": 0,
                "decayed_nodes": 0,
                "decayed_edges": 0,
                "skipped_reason": "no_change",
            }
        self._rewrite_events_from_state(
            bounded_nodes,
            bounded_edges,
            control_event=_control_event(
                "ghost_affinity_state_decayed",
                {
                    "removed_nodes": removed_nodes,
                    "removed_edges": removed_edges,
                    "decayed_nodes": decayed_node_count,
                    "decayed_edges": decayed_edge_count,
                },
            ),
        )
        self._write_projection(bounded_nodes, bounded_edges, warnings=[])
        return {
            "removed_nodes": removed_nodes,
            "removed_edges": removed_edges,
            "decayed_nodes": decayed_node_count,
            "decayed_edges": decayed_edge_count,
            "skipped_reason": "",
        }

    def compact_if_needed(self) -> dict[str, object]:
        before = _event_file_stats(self.events_path, max_bytes=MAX_AFFINITY_EVENTS_BYTES)
        if not self.events_path.exists() and self.projection_path.exists():
            warning = "affinity_events_missing"
            self.last_warnings = (warning,)
            return _compact_payload(False, False, before, before, (warning,))
        if not before["readable"]:
            warning = str(before["warning"] or "affinity_events_unreadable")
            self.last_warnings = (warning,)
            return _compact_payload(False, False, before, before, (warning,))
        if before["events"] <= MAX_AFFINITY_EVENTS and before["bytes"] <= MAX_AFFINITY_EVENTS_BYTES:
            return _compact_payload(True, False, before, before, self.last_warnings)
        nodes, edges = self._load_state_for_mutation()
        if self._events_read_blocked:
            return _compact_payload(False, False, before, before, self.last_warnings)
        self._rewrite_events_from_state(nodes, edges)
        after = _event_file_stats(self.events_path, max_bytes=MAX_AFFINITY_EVENTS_BYTES)
        return _compact_payload(True, after != before, before, after, self.last_warnings)

    def _source_specs(
        self,
        *,
        hebbian_store: Any,
        work_queue_store: Any,
        research_interest_candidates: Iterable[Any],
        router_store: Any,
        run_projection: Any,
        terminal_event: Mapping[str, object] | None,
        session_id: str,
        project: str,
    ) -> tuple[list[_NodeSpec], list[_EdgeSpec]]:
        node_specs: list[_NodeSpec] = []
        edge_specs: list[_EdgeSpec] = []
        node_specs.extend(_node_specs_from_hebbian(hebbian_store))
        work_nodes, work_edges = _specs_from_work_queue(work_queue_store, session_id=session_id, project=project)
        node_specs.extend(work_nodes)
        edge_specs.extend(work_edges)
        research_nodes, research_edges = _specs_from_research_candidates(
            research_interest_candidates,
            session_id=session_id,
            project=project,
        )
        node_specs.extend(research_nodes)
        edge_specs.extend(research_edges)
        router_nodes, router_edges = _specs_from_router(router_store)
        node_specs.extend(router_nodes)
        edge_specs.extend(router_edges)
        provider_nodes, provider_edges = _specs_from_provider_outcome(
            run_projection=run_projection,
            terminal_event=terminal_event,
            session_id=session_id,
            project=project,
        )
        node_specs.extend(provider_nodes)
        edge_specs.extend(provider_edges)
        return node_specs, edge_specs

    def _load_state_for_read(self) -> tuple[list[AffinityNode], list[AffinityEdge]]:
        if self.events_path.exists():
            events = self._read_events()
            if not self._events_read_blocked:
                return _rows_from_events(events)
            return self._load_projection_rows()
        return self._load_projection_rows()

    def _load_state_for_hint(self) -> tuple[list[AffinityNode], list[AffinityEdge]]:
        if self.events_path.exists():
            events = self._read_events()
            if self._events_read_blocked:
                return [], []
            return _rows_from_events(events)
        if self.projection_path.exists():
            self.last_warnings = ("affinity_events_missing",)
        return [], []

    def _load_state_for_mutation(self) -> tuple[list[AffinityNode], list[AffinityEdge]]:
        if self.events_path.exists():
            events = self._read_events()
            if self._events_read_blocked:
                return [], []
            return _rows_from_events(events)
        if self.projection_path.exists():
            self._events_read_blocked = True
            self._events_blocked_reason = "affinity_events_missing"
            self.last_warnings = ("affinity_events_missing",)
            return [], []
        self._events_read_blocked = False
        self._events_blocked_reason = ""
        self.last_warnings = ()
        return [], []

    def _load_projection_rows(self) -> tuple[list[AffinityNode], list[AffinityEdge]]:
        payload = read_json(self.projection_path, max_bytes=MAX_AFFINITY_STATE_BYTES)
        if not isinstance(payload, Mapping):
            return [], []
        if payload.get("schema_version") != AFFINITY_SCHEMA_VERSION:
            return [], []
        if payload.get("kind") != _STATE_KIND:
            return [], []
        nodes = [
            node for node in (AffinityNode.from_payload(row) for row in _list(payload.get("nodes")))
            if node is not None
        ]
        node_ids = {node.id for node in nodes}
        edges = [
            edge for edge in (AffinityEdge.from_payload(row) for row in _list(payload.get("edges")))
            if edge is not None and edge.source in node_ids and edge.target in node_ids
        ]
        return _bounded_nodes(nodes), _bounded_edges(edges, node_ids=node_ids)

    def _read_events(self) -> list[dict[str, object]]:
        self._events_read_blocked = False
        self._events_blocked_reason = ""
        try:
            if not self.events_path.is_file():
                self.last_warnings = ()
                return []
            if self.events_path.stat().st_size > MAX_AFFINITY_EVENTS_BYTES:
                self.last_warnings = ("affinity_events_too_large",)
                self._events_read_blocked = True
                self._events_blocked_reason = "events_read_blocked"
                return []
            lines = self.events_path.read_text(encoding="utf-8").splitlines()
        except (OSError, UnicodeDecodeError):
            self.last_warnings = ("affinity_events_unreadable",)
            self._events_read_blocked = True
            self._events_blocked_reason = "events_read_blocked"
            return []
        rows: list[dict[str, object]] = []
        warnings: list[str] = []
        for index, line in enumerate(lines, start=1):
            if not line.strip():
                continue
            try:
                payload = json.loads(line)
            except json.JSONDecodeError:
                warnings.append(f"affinity_events.jsonl:{index}:bad_json")
                continue
            if not isinstance(payload, dict):
                warnings.append(f"affinity_events.jsonl:{index}:not_object")
                continue
            if payload.get("schema_version") != AFFINITY_SCHEMA_VERSION:
                warnings.append(f"affinity_events.jsonl:{index}:unsupported_schema")
                continue
            rows.append(payload)
        self.last_warnings = _bounded_warnings(warnings)
        return rows

    def _append_events_atomic(self, events: Iterable[dict[str, object]]) -> bool:
        rows = [event for event in events if isinstance(event, dict)]
        if not rows:
            return True
        try:
            if not self.events_path.exists() and self.projection_path.exists():
                self.last_warnings = ("affinity_events_missing",)
                self._events_read_blocked = True
                self._events_blocked_reason = "affinity_events_missing"
                return False
            existing = self._read_events() if self.events_path.exists() else []
            if self._events_read_blocked:
                return False
            self._write_events_atomic([*existing, *rows])
            return True
        except (OSError, TypeError, ValueError):
            self.last_warnings = ("affinity_event_write_failed",)
            return False

    def _write_events_atomic(self, events: Iterable[dict[str, object]]) -> None:
        rows = [event for event in events if isinstance(event, dict)]
        data = "".join(_json_line(event) for event in rows).encode("utf-8")
        if len(data) > MAX_AFFINITY_EVENTS_BYTES:
            raise ValueError("ghost affinity events are too large")
        self.directory.mkdir(parents=True, exist_ok=True)
        temporary = self.events_path.with_name(f".{self.events_path.name}.{uuid.uuid4().hex}.tmp")
        try:
            with temporary.open("xb") as handle:
                handle.write(data)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, self.events_path)
        finally:
            try:
                temporary.unlink()
            except OSError:
                pass

    def _rewrite_events_from_state(
        self,
        nodes: Iterable[AffinityNode],
        edges: Iterable[AffinityEdge],
        *,
        control_event: dict[str, object] | None = None,
    ) -> None:
        node_rows = _bounded_nodes(nodes)
        node_ids = {node.id for node in node_rows}
        edge_rows = _bounded_edges(edges, node_ids=node_ids)
        events = [_node_event(node, action="compacted") for node in node_rows]
        events.extend(_edge_event(edge, action="compacted") for edge in edge_rows)
        events.append(control_event or _control_event("ghost_affinity_events_compacted", {"nodes": len(node_rows), "edges": len(edge_rows)}))
        self._write_events_atomic(events)

    def _write_projection(
        self,
        nodes: Iterable[AffinityNode],
        edges: Iterable[AffinityEdge],
        *,
        warnings: Iterable[str],
    ) -> None:
        write_json_atomic(
            self.projection_path,
            _projection_payload(nodes, edges, generated_at=_now(), warnings=warnings),
            max_bytes=MAX_AFFINITY_STATE_BYTES,
        )

    def _write_projection_best_effort(
        self,
        nodes: Iterable[AffinityNode],
        edges: Iterable[AffinityEdge],
    ) -> None:
        try:
            self._write_projection(nodes, edges, warnings=self.last_warnings)
        except (OSError, TypeError, ValueError):
            try:
                delete_file(self.projection_path)
            except OSError:
                pass
            self.last_warnings = _bounded_warnings((*self.last_warnings, "affinity_projection_write_failed"))

    def _compact_if_needed(self, nodes: Iterable[AffinityNode], edges: Iterable[AffinityEdge]) -> None:
        stats = _event_file_stats(self.events_path, max_bytes=MAX_AFFINITY_EVENTS_BYTES)
        if not stats["readable"]:
            self.last_warnings = (str(stats["warning"] or "affinity_events_unreadable"),)
            return
        if stats["events"] <= MAX_AFFINITY_EVENTS and stats["bytes"] <= MAX_AFFINITY_EVENTS_BYTES:
            return
        try:
            self._rewrite_events_from_state(nodes, edges)
        except (OSError, TypeError, ValueError):
            self.last_warnings = _bounded_warnings((*self.last_warnings, "affinity_compaction_failed"))

    def _sync_failed(self, reason: str) -> GhostAffinitySyncResult:
        warnings = self.last_warnings or ((reason,) if reason else ())
        self.last_warnings = _bounded_warnings(warnings)
        return GhostAffinitySyncResult(False, skipped_reason=reason, warnings=self.last_warnings)


def apply_affinity_work_boost(priority: float, hints: Iterable[Any], target: str) -> float:
    base = _unit_float(priority)
    boost = _hint_boost(hints, target, maximum=0.12)
    return _unit_float(base + boost)


def apply_affinity_research_boost(priority: float, hints: Iterable[Any], target: str) -> float:
    base = _unit_float(priority)
    boost = _hint_boost(hints, target, maximum=0.14)
    return _unit_float(base + boost)


def _hint_boost(hints: Iterable[Any], target: str, *, maximum: float) -> float:
    clean_target = clip_signal_text(target, 120)
    boost = 0.0
    for hint in list(hints or []):
        if clip_signal_text(_field(hint, "target"), 120) != clean_target:
            continue
        weight = _unit_float(_field(hint, "weight"))
        confidence = _unit_float(_field(hint, "confidence"))
        boost = max(boost, weight * confidence * maximum)
    return min(maximum, boost)


def _node_specs_from_hebbian(hebbian_store: Any) -> list[_NodeSpec]:
    if hebbian_store is None:
        return []
    try:
        rows = hebbian_store.list_nodes(status="active")
    except Exception:
        return []
    specs: list[_NodeSpec] = []
    for node in rows:
        if str(getattr(node, "status", "")) != "active" or getattr(node, "superseded_by", ""):
            continue
        affinity_kind = _HEBBIAN_KIND_MAP.get(str(getattr(node, "kind", "") or ""))
        if not affinity_kind:
            continue
        scope, scope_ref = _scope_from_source(node)
        conflict_key = _clean_key(getattr(node, "conflict_key", ""), 120)
        value_key = _clean_key(getattr(node, "value_key", ""), 120)
        if not conflict_key or not value_key:
            continue
        key = _clean_key(f"{getattr(node, 'kind', '')}:{conflict_key}:{value_key}", 180)
        label = _clean_label(f"{getattr(node, 'kind', '')}:{conflict_key}={value_key}", 180)
        source_refs = _bounded_refs((
            f"hebbian_node:{clip_signal_text(getattr(node, 'id', ''), 120)}",
            *(f"hebbian_evidence:{ref}" for ref in _list(getattr(node, "evidence_refs", ()))),
        ))
        if not key or not label or not source_refs:
            continue
        evidence_refs = _bounded_refs(tuple(
            f"hebbian:{ref}"
            for ref in _list(getattr(node, "evidence_refs", ()))
        ))
        specs.append(_NodeSpec(
            kind=affinity_kind,
            key=key,
            label=label,
            scope=scope,
            scope_ref=scope_ref,
            confidence=_unit_float(getattr(node, "confidence", 0.0)),
            reward=max(0.2, _unit_float(getattr(node, "weight", 0.0))),
            source_refs=source_refs,
            evidence_refs=evidence_refs,
            metadata={
                "source": "hebbian",
                "hebbian_node_id": clip_signal_text(getattr(node, "id", ""), 120),
                "hebbian_kind": clip_signal_text(getattr(node, "kind", ""), 80),
                "conflict_key": conflict_key,
                "value_key": value_key,
            },
        ))
    return specs


def _specs_from_work_queue(
    work_queue_store: Any,
    *,
    session_id: str,
    project: str,
) -> tuple[list[_NodeSpec], list[_EdgeSpec]]:
    if work_queue_store is None:
        return [], []
    try:
        rows = work_queue_store.list_items()
    except Exception:
        return [], []
    node_specs: list[_NodeSpec] = []
    edge_specs: list[_EdgeSpec] = []
    for item in rows:
        status = clip_signal_text(_field(item, "status"), 40)
        reward = _WORK_STATUS_REWARD.get(status)
        if reward is None:
            continue
        scope, scope_ref = _scope_from_source(item, fallback_session_id=session_id, fallback_project=project)
        item_id = clip_signal_text(_field(item, "id"), 120)
        task_kind = _clean_key(_field(item, "kind"), 80)
        if not item_id or not task_kind:
            continue
        item_ref = _bounded_refs((f"work_item:{item_id}:{status}:{clip_signal_text(_field(item, 'updated_at'), 80)}",))
        task_node = _NodeSpec(
            kind="task_type",
            key=task_kind,
            label=f"task_type:{task_kind}",
            scope=scope,
            scope_ref=scope_ref,
            confidence=_unit_float(_field(item, "confidence")),
            reward=reward,
            source_refs=item_ref,
            metadata={"source": "work_queue", "work_status": status},
        )
        node_specs.append(task_node)
        task_id = _node_id(task_node.kind, task_node.scope, task_node.scope_ref, task_node.key)
        if scope == "project" and scope_ref:
            project_key_value = scope_ref
            project_node = _NodeSpec(
                kind="project",
                key=project_key_value,
                label=f"project:{project_key_value}",
                scope=scope,
                scope_ref=scope_ref,
                confidence=_unit_float(_field(item, "confidence")),
                reward=reward,
                source_refs=item_ref,
                metadata={"source": "work_queue"},
            )
            node_specs.append(project_node)
            project_id = _node_id(project_node.kind, project_node.scope, project_node.scope_ref, project_node.key)
            edge_specs.append(_EdgeSpec(
                source=project_id,
                target=task_id,
                relation="used_in_task",
                scope=scope,
                scope_ref=scope_ref,
                confidence=_unit_float(_field(item, "confidence")),
                reward=reward,
                source_refs=item_ref,
                proof_refs=_bounded_refs(_field(item, "proof_refs")) if status == "done" else (),
            ))
        relation = "works_well_for" if status == "done" else "struggles_with" if status == "blocked" else ""
        if relation and scope == "project" and scope_ref:
            provider_or_project = _node_id("project", scope, scope_ref, scope_ref)
            edge_specs.append(_EdgeSpec(
                source=task_id,
                target=provider_or_project,
                relation=relation,
                scope=scope,
                scope_ref=scope_ref,
                confidence=_unit_float(_field(item, "confidence")),
                reward=reward,
                source_refs=item_ref,
                proof_refs=_bounded_refs(_field(item, "proof_refs")) if status == "done" else item_ref,
            ))
        for concept in _concepts_from_work_item(item):
            concept_node = _NodeSpec(
                kind="research_concept",
                key=concept,
                label=f"concept:{concept}",
                scope=scope,
                scope_ref=scope_ref,
                confidence=_unit_float(_field(item, "confidence")),
                reward=min(0.8, reward),
                source_refs=item_ref,
                metadata={"source": "work_queue", "not_evidence": True},
            )
            node_specs.append(concept_node)
            concept_id = _node_id(concept_node.kind, concept_node.scope, concept_node.scope_ref, concept_node.key)
            edge_specs.append(_EdgeSpec(
                source=task_id,
                target=concept_id,
                relation="mentions_concept",
                scope=scope,
                scope_ref=scope_ref,
                confidence=_unit_float(_field(item, "confidence")),
                reward=min(0.8, reward),
                source_refs=item_ref,
                proof_refs=_bounded_refs(_field(item, "proof_refs")) if status == "done" else (),
            ))
    return node_specs, edge_specs


def _specs_from_research_candidates(
    candidates: Iterable[Any],
    *,
    session_id: str,
    project: str,
) -> tuple[list[_NodeSpec], list[_EdgeSpec]]:
    node_specs: list[_NodeSpec] = []
    edge_specs: list[_EdgeSpec] = []
    for candidate in list(candidates or []):
        candidate_id = clip_signal_text(_field(candidate, "id"), 120)
        if not candidate_id:
            continue
        scope, scope_ref = _scope_from_source(candidate, fallback_session_id=session_id, fallback_project=project)
        confidence = _unit_float(_field(candidate, "confidence"))
        reward = max(0.35, _unit_float(_field(candidate, "priority")))
        refs = _bounded_refs((
            f"research_interest:{candidate_id}",
            *_list(_field(candidate, "source_refs")),
        ))
        concepts = _concepts_from_candidate(candidate)
        concept_ids: list[str] = []
        for concept in concepts:
            node_spec = _NodeSpec(
                kind="research_concept",
                key=concept,
                label=f"concept:{concept}",
                scope=scope,
                scope_ref=scope_ref,
                confidence=confidence,
                reward=reward,
                source_refs=refs,
                evidence_refs=(),
                metadata={
                    "source": clip_signal_text(_field(candidate, "source"), 80),
                    "not_evidence": True,
                },
            )
            node_specs.append(node_spec)
            concept_ids.append(_node_id(node_spec.kind, node_spec.scope, node_spec.scope_ref, node_spec.key))
        if len(concept_ids) >= 2:
            for index, source in enumerate(concept_ids):
                for target in concept_ids[index + 1:]:
                    edge_specs.append(_EdgeSpec(
                        source=source,
                        target=target,
                        relation="associated_with",
                        scope=scope,
                        scope_ref=scope_ref,
                        confidence=confidence,
                        reward=reward,
                        source_refs=refs,
                        proof_refs=(),
                    ))
    return node_specs, edge_specs


def _specs_from_router(router_store: Any) -> tuple[list[_NodeSpec], list[_EdgeSpec]]:
    if router_store is None:
        return [], []
    try:
        exported = router_store.export_state()
        records = _list((exported.get("router") if isinstance(exported, Mapping) else {}).get("records"))
    except Exception:
        return [], []
    node_specs: list[_NodeSpec] = []
    edge_specs: list[_EdgeSpec] = []
    for record in records:
        if not isinstance(record, Mapping) or not bool(record.get("ok", True)):
            continue
        final_mode = _clean_key(record.get("final_mode"), 80)
        baseline_mode = _clean_key(record.get("baseline_mode"), 80)
        if not final_mode or not baseline_mode:
            continue
        if clip_signal_text(record.get("session_ref"), 120):
            scope = "session"
            scope_ref = clip_signal_text(record.get("session_ref"), 120)
        elif clip_signal_text(record.get("project_ref"), 120):
            scope = "project"
            scope_ref = clip_signal_text(record.get("project_ref"), 120)
        else:
            scope = "user"
            scope_ref = ""
        refs = _bounded_refs((f"router:{clip_signal_text(record.get('run_id'), 120)}:{clip_signal_text(record.get('task_hash'), 80)}:{final_mode}",))
        confidence = _unit_float(record.get("confidence"))
        final_spec = _NodeSpec(
            kind="task_type",
            key=f"mode:{final_mode}",
            label=f"mode:{final_mode}",
            scope=scope,
            scope_ref=scope_ref,
            confidence=confidence,
            reward=0.35,
            source_refs=refs,
            metadata={"source": "router", "reason_code": clip_signal_text(record.get("reason"), 80)},
        )
        node_specs.append(final_spec)
        if final_mode != baseline_mode:
            baseline_spec = _NodeSpec(
                kind="task_type",
                key=f"mode:{baseline_mode}",
                label=f"mode:{baseline_mode}",
                scope=scope,
                scope_ref=scope_ref,
                confidence=confidence,
                reward=0.2,
                source_refs=refs,
                metadata={"source": "router"},
            )
            node_specs.append(baseline_spec)
            edge_specs.append(_EdgeSpec(
                source=_node_id(baseline_spec.kind, baseline_spec.scope, baseline_spec.scope_ref, baseline_spec.key),
                target=_node_id(final_spec.kind, final_spec.scope, final_spec.scope_ref, final_spec.key),
                relation="associated_with",
                scope=scope,
                scope_ref=scope_ref,
                confidence=confidence,
                reward=0.25,
                source_refs=refs,
            ))
    return node_specs, edge_specs


def _specs_from_provider_outcome(
    *,
    run_projection: Any,
    terminal_event: Mapping[str, object] | None,
    session_id: str,
    project: str,
) -> tuple[list[_NodeSpec], list[_EdgeSpec]]:
    if run_projection is None and not isinstance(terminal_event, Mapping):
        return [], []
    node_specs: list[_NodeSpec] = []
    edge_specs: list[_EdgeSpec] = []
    failures = []
    if run_projection is not None:
        failures.extend(list(getattr(run_projection, "provider_failures", ()) or ()))
    if isinstance(terminal_event, Mapping) and isinstance(terminal_event.get("provider_failure"), Mapping):
        failures.append(terminal_event.get("provider_failure"))
    scope = "project" if project else "session" if session_id else "user"
    scope_ref = _scope_ref(scope, project or session_id)
    task_mode = _clean_key(
        getattr(run_projection, "mode", "")
        or (terminal_event.get("mode") if isinstance(terminal_event, Mapping) else "")
        or "task",
        80,
    )
    run_ref = _bounded_refs((f"run:{clip_signal_text(getattr(run_projection, 'run_id', '') or (terminal_event or {}).get('run_id'), 120)}",))
    task_spec = _NodeSpec(
        kind="task_type",
        key=task_mode,
        label=f"task_type:{task_mode}",
        scope=scope,
        scope_ref=scope_ref,
        confidence=0.6,
        reward=0.2,
        source_refs=run_ref,
    )
    if task_mode and run_ref:
        node_specs.append(task_spec)
    task_id = _node_id(task_spec.kind, task_spec.scope, task_spec.scope_ref, task_spec.key) if task_mode else ""
    for failure in failures:
        provider = _clean_key(_field(failure, "provider") or _field(failure, "model") or (terminal_event or {}).get("provider"), 80)
        error_kind = _clean_provider_error_kind(_field(failure, "kind"))
        action = _clean_key(_field(failure, "action"), 80)
        stage = _clean_key(_field(failure, "stage"), 80)
        if not provider or not error_kind:
            continue
        refs = _bounded_refs((
            "provider_failure:"
            + hashlib.sha256(
                "|".join((
                    clip_signal_text(getattr(run_projection, "run_id", "") or (terminal_event or {}).get("run_id"), 120),
                    provider,
                    error_kind,
                    action,
                    stage,
                )).encode("utf-8", errors="replace")
            ).hexdigest()[:24],
        ))
        if not refs:
            continue
        provider_spec = _NodeSpec(
            kind="provider_behavior",
            key=f"{provider}:{error_kind}:{action}:{stage}",
            label=f"provider:{provider}:{error_kind}",
            scope=scope,
            scope_ref=scope_ref,
            confidence=0.75,
            reward=0.55,
            source_refs=refs,
            metadata={
                "source": "provider_failure",
                "provider": provider,
                "error_kind": error_kind,
                "action": action,
                "stage": stage,
            },
        )
        node_specs.append(provider_spec)
        if task_id:
            edge_specs.append(_EdgeSpec(
                source=_node_id(provider_spec.kind, provider_spec.scope, provider_spec.scope_ref, provider_spec.key),
                target=task_id,
                relation="struggles_with",
                scope=scope,
                scope_ref=scope_ref,
                confidence=0.75,
                reward=0.45,
                source_refs=refs,
            ))
    return node_specs, edge_specs


def _reinforce_node(current: AffinityNode | None, spec: _NodeSpec, *, now: str) -> tuple[AffinityNode, bool]:
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
        increment_ref_count = max(1, len(new_source_refs) + len(new_evidence_refs))
    else:
        old_weight = 0.0
        created_at = now
        confidence = _unit_float(spec.confidence)
        metadata = _clean_metadata(spec.metadata)
        increment_ref_count = max(1, len(source_refs) + len(evidence_refs))
    increment = NODE_LEARNING_RATE * _unit_float(spec.reward) * max(confidence, 0.1) * increment_ref_count
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


def _reinforce_edge(current: AffinityEdge | None, spec: _EdgeSpec, *, now: str) -> tuple[AffinityEdge, bool]:
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
        increment_ref_count = max(1, len(new_source_refs) + len(new_proof_refs))
    else:
        old_weight = 0.0
        created_at = now
        confidence = _unit_float(spec.confidence)
        increment_ref_count = max(1, len(source_refs) + len(proof_refs))
    increment = EDGE_LEARNING_RATE * _unit_float(spec.reward) * max(confidence, 0.1) * increment_ref_count
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
    decayed = _decayed_weight(node.weight, _decay_basis(node), now, NODE_HALF_LIFE_DAYS)
    status = "expired" if node.status == "active" and decayed < MIN_NODE_WEIGHT else node.status
    return replace(node, weight=decayed, status=status, updated_at=now, last_decayed_at=now)


def _decay_edge(edge: AffinityEdge, *, now: str) -> AffinityEdge:
    decayed = _decayed_weight(edge.weight, _decay_basis(edge), now, EDGE_HALF_LIFE_DAYS)
    status = "expired" if decayed < MIN_EDGE_WEIGHT else edge.status
    return replace(edge, weight=decayed, status=status, updated_at=now, last_decayed_at=now)


def _decay_basis(row: AffinityNode | AffinityEdge) -> str:
    return row.last_decayed_at or row.last_reinforced_at or row.updated_at


def _decayed_weight(weight: float, basis: str, now: str, half_life_days: float) -> float:
    age = max(0.0, (_parse_ts(now) - _parse_ts(basis)).total_seconds())
    half_life_seconds = max(1.0, float(half_life_days) * 24.0 * 60.0 * 60.0)
    return _unit_float(float(weight or 0.0) * math.exp(-(math.log(2.0) / half_life_seconds) * age))


def _any_decay_due(
    rows: Iterable[AffinityNode | AffinityEdge],
    *,
    now: str,
    min_interval_seconds: int,
) -> bool:
    threshold = max(0, int(min_interval_seconds or 0))
    if threshold <= 0:
        return True
    now_ts = _parse_ts(now)
    for row in rows:
        age = max(0.0, (now_ts - _parse_ts(_decay_basis(row))).total_seconds())
        if age >= threshold:
            return True
    return False


def _bounded_nodes(nodes: Iterable[AffinityNode]) -> list[AffinityNode]:
    rows = [
        node for node in nodes
        if isinstance(node, AffinityNode) and (node.status != "active" or node.weight >= MIN_NODE_WEIGHT)
    ]
    rows.sort(key=lambda item: (item.status == "active", item.weight, item.updated_at), reverse=True)
    return rows[:MAX_AFFINITY_NODES]


def _bounded_edges(edges: Iterable[AffinityEdge], *, node_ids: set[str]) -> list[AffinityEdge]:
    rows = [
        edge for edge in edges
        if isinstance(edge, AffinityEdge)
        and edge.source in node_ids
        and edge.target in node_ids
        and edge.status == "active"
        and edge.weight >= MIN_EDGE_WEIGHT
    ]
    rows.sort(key=lambda item: (item.weight, item.updated_at), reverse=True)
    bounded: list[AffinityEdge] = []
    degree: dict[str, int] = {}
    for edge in rows:
        if len(bounded) >= MAX_AFFINITY_EDGES:
            break
        if degree.get(edge.source, 0) >= MAX_EDGE_OUT_DEGREE:
            continue
        if degree.get(edge.target, 0) >= MAX_EDGE_OUT_DEGREE:
            continue
        bounded.append(edge)
        degree[edge.source] = degree.get(edge.source, 0) + 1
        degree[edge.target] = degree.get(edge.target, 0) + 1
    return bounded


def _rows_from_events(events: Iterable[dict[str, object]]) -> tuple[list[AffinityNode], list[AffinityEdge]]:
    nodes: dict[str, AffinityNode] = {}
    edges: dict[str, AffinityEdge] = {}
    for event in events:
        event_type = str(event.get("type") or "")
        if event_type == "ghost_affinity_node_upsert":
            node = AffinityNode.from_payload(event.get("node"))
            if node is not None:
                nodes[node.id] = node
        elif event_type == "ghost_affinity_edge_upsert":
            edge = AffinityEdge.from_payload(event.get("edge"))
            if edge is not None:
                edges[edge.id] = edge
    bounded_nodes = _bounded_nodes(nodes.values())
    bounded_edges = _bounded_edges(edges.values(), node_ids={node.id for node in bounded_nodes})
    return bounded_nodes, bounded_edges


def _projection_payload(
    nodes: Iterable[AffinityNode],
    edges: Iterable[AffinityEdge],
    *,
    generated_at: str,
    warnings: Iterable[str],
) -> dict[str, object]:
    node_rows = _bounded_nodes(nodes)
    edge_rows = _bounded_edges(edges, node_ids={node.id for node in node_rows})
    return {
        "schema_version": AFFINITY_SCHEMA_VERSION,
        "kind": _STATE_KIND,
        "source": "affinity_events.jsonl",
        "generated_at": generated_at,
        "nodes": [node.to_payload() for node in node_rows],
        "edges": [edge.to_payload() for edge in edge_rows],
        "warnings": list(_bounded_warnings(warnings)),
    }


def _node_event(node: AffinityNode, *, action: str) -> dict[str, object]:
    return {
        "schema_version": AFFINITY_SCHEMA_VERSION,
        "type": "ghost_affinity_node_upsert",
        "event_id": "gae_" + uuid.uuid4().hex[:24],
        "ts": _now(),
        "action": clip_signal_text(action, 40),
        "node": node.to_payload(),
    }


def _edge_event(edge: AffinityEdge, *, action: str) -> dict[str, object]:
    return {
        "schema_version": AFFINITY_SCHEMA_VERSION,
        "type": "ghost_affinity_edge_upsert",
        "event_id": "gae_" + uuid.uuid4().hex[:24],
        "ts": _now(),
        "action": clip_signal_text(action, 40),
        "edge": edge.to_payload(),
    }


def _control_event(event_type: str, payload: Mapping[str, object]) -> dict[str, object]:
    return {
        "schema_version": AFFINITY_SCHEMA_VERSION,
        "type": clip_signal_text(event_type, 80),
        "event_id": "gac_" + uuid.uuid4().hex[:24],
        "ts": _now(),
        "payload": _clean_metadata(payload),
    }


def _node_id(kind: str, scope: str, scope_ref: str, key: str) -> str:
    raw = "|".join((_clean_node_kind(kind), _clean_scope(scope), clip_signal_text(scope_ref, 120), _clean_key(key, 180)))
    return "gan_" + hashlib.sha256(raw.encode("utf-8", errors="replace")).hexdigest()[:24]


def _edge_id(source: str, target: str, relation: str, scope: str, scope_ref: str) -> str:
    clean_source = clip_signal_text(source, 120)
    clean_target = clip_signal_text(target, 120)
    clean_relation = _clean_relation(relation)
    if clean_relation == "associated_with":
        clean_source, clean_target = sorted((clean_source, clean_target))
    raw = "|".join((clean_source, clean_target, clean_relation, _clean_scope(scope), clip_signal_text(scope_ref, 120)))
    return "gae_" + hashlib.sha256(raw.encode("utf-8", errors="replace")).hexdigest()[:24]


def _scope_from_source(
    value: Any,
    *,
    fallback_session_id: str = "",
    fallback_project: str = "",
) -> tuple[str, str]:
    scope = _clean_scope(_field(value, "scope"))
    raw_ref = clip_signal_text(_field(value, "scope_ref"), 240)
    if not scope:
        scope = "session" if fallback_session_id else "project" if fallback_project else "user"
        raw_ref = fallback_session_id or fallback_project
    if scope == "session":
        raw_ref = raw_ref or clip_signal_text(_field(value, "session_id"), 120) or fallback_session_id
    elif scope == "project":
        raw_ref = raw_ref or clip_signal_text(_field(value, "project"), 240) or fallback_project
    return scope, _scope_ref(scope, raw_ref)


def _scope_ref(scope: str, raw_ref: object) -> str:
    clean_scope = _clean_scope(scope)
    text = clip_signal_text(raw_ref, 240)
    if clean_scope == "session":
        return text if _looks_like_hash_ref(text) else session_key(text) if text else ""
    if clean_scope == "project":
        if not text:
            return ""
        if _looks_like_hash_ref(text):
            return text
        try:
            return project_key(text)
        except (OSError, RuntimeError, ValueError):
            return hashlib.sha256(text.casefold().encode("utf-8", errors="replace")).hexdigest()[:24]
    return ""


def _scope_ref_for_filter(scope: str, *, project: str, session_id: str) -> str:
    if scope == "project":
        return _scope_ref("project", project)
    if scope == "session":
        return _scope_ref("session", session_id)
    return ""


def _scope_visible_for_filter(
    row_scope: str,
    row_scope_ref: str,
    *,
    scope: str = "",
    project: str = "",
    session_id: str = "",
) -> bool:
    clean_scope = _clean_scope(row_scope)
    requested_scope = _clean_scope(scope)
    if requested_scope:
        if clean_scope != requested_scope:
            return False
        requested_ref = _scope_ref_for_filter(requested_scope, project=project, session_id=session_id)
        if requested_scope in {"project", "session"}:
            return bool(requested_ref and row_scope_ref == requested_ref)
        return requested_scope == "user"
    if clean_scope == "user":
        return True
    if clean_scope == "project":
        return bool(project and row_scope_ref == _scope_ref("project", project))
    if clean_scope == "session":
        return bool(session_id and row_scope_ref == _scope_ref("session", session_id))
    return False


def _scope_matches_values(scope: str, scope_ref: str, *, project: str, session_id: str) -> bool:
    clean_scope = _clean_scope(scope)
    if clean_scope == "session":
        return bool(session_id and scope_ref == _scope_ref("session", session_id))
    if clean_scope == "project":
        return bool(project and scope_ref == _scope_ref("project", project))
    return clean_scope == "user"


def _looks_like_hash_ref(value: object) -> bool:
    text = str(value or "").strip()
    return len(text) == 24 and all(ch in "0123456789abcdef" for ch in text)


def _concepts_from_work_item(item: Any) -> tuple[str, ...]:
    metadata = _field(item, "metadata")
    if not isinstance(metadata, Mapping):
        return ()
    return _clean_concepts((
        *_metadata_sequence(metadata.get("related_concepts")),
        *_metadata_sequence(metadata.get("shared_neighbors")),
    ))


def _concepts_from_candidate(candidate: Any) -> tuple[str, ...]:
    return _clean_concepts((*_list(_field(candidate, "related_concepts")), *_list(_field(candidate, "shared_neighbors"))))


def _clean_concepts(values: Iterable[object]) -> tuple[str, ...]:
    out: list[str] = []
    for value in values:
        text = _clean_key(value, 120).casefold()
        if not text or text in out:
            continue
        out.append(text)
        if len(out) >= 8:
            break
    return tuple(out)


def _hint(
    kind: str,
    target: str,
    weight: float,
    confidence: float,
    reason_code: str,
    source_refs: Iterable[object],
    warnings: Iterable[object] = (),
) -> AffinityHint:
    return AffinityHint(
        kind=_clean_hint_kind(kind),
        target=clip_signal_text(target, 120),
        weight=_unit_float(weight),
        confidence=_unit_float(confidence),
        reason_code=_clean_key(reason_code, 80),
        source_refs=_bounded_refs(source_refs, limit=MAX_AFFINITY_HINT_REFS),
        warnings=_bounded_warnings(warnings),
    )


def _bounded_hints(hints: Iterable[AffinityHint]) -> tuple[AffinityHint, ...]:
    rows = [hint for hint in hints if hint.kind and hint.target and hint.weight > 0.0]
    rows.sort(key=lambda item: (item.weight, item.confidence, item.target), reverse=True)
    return tuple(rows[:MAX_HINTS])


def _clean_node_kind(value: object) -> str:
    text = str(value or "").strip().lower()
    return text if text in AFFINITY_NODE_KINDS else ""


def _clean_node_status(value: object) -> str:
    text = str(value or "").strip().lower()
    return text if text in AFFINITY_NODE_STATUSES else ""


def _clean_edge_status(value: object) -> str:
    text = str(value or "").strip().lower()
    return text if text in AFFINITY_EDGE_STATUSES else ""


def _clean_relation(value: object) -> str:
    text = str(value or "").strip().lower()
    return text if text in AFFINITY_EDGE_RELATIONS else ""


def _clean_scope(value: object) -> str:
    text = str(value or "").strip().lower()
    return text if text in AFFINITY_SCOPES else ""


def _clean_hint_kind(value: object) -> str:
    text = str(value or "").strip().lower()
    return text if text in HINT_KINDS else ""


def _clean_provider_error_kind(value: object) -> str:
    text = _clean_key(value, 80)
    return text if text in _PROVIDER_ERROR_KINDS else "transient" if text else ""


def _clean_key(value: object, limit: int = 180) -> str:
    text = " ".join(str(value or "").replace("\r\n", "\n").replace("\r", "\n").split())
    text = clip_signal_text(text, limit).strip().strip(".")
    if not text or contains_sensitive_signal_text(text):
        return ""
    return text


def _clean_label(value: object, limit: int = 180) -> str:
    text = _clean_key(value, limit)
    if not text:
        return ""
    lower = text.casefold()
    if "prompt" in lower or "raw" in lower or "source body" in lower:
        return ""
    return text


def _clean_metadata(value: object) -> dict[str, object]:
    if not isinstance(value, Mapping):
        return {}
    out: dict[str, object] = {}
    for key, item in value.items():
        clean_key = _clean_key(key, 80)
        if not clean_key:
            continue
        if isinstance(item, bool) or item is None:
            clean_item: object = item
        elif isinstance(item, int):
            clean_item = int(item)
        elif isinstance(item, float):
            clean_item = _unit_float(item)
        else:
            clean_item = _clean_key(item, 180)
        if isinstance(clean_item, str) and not clean_item:
            continue
        out[clean_key] = clean_item
        if len(out) >= 16:
            break
    return out


def _bounded_refs(values: object, *, limit: int = MAX_AFFINITY_REFS) -> tuple[str, ...]:
    return _merge_refs((), _list(values), limit=limit)


def _ref_hash(value: object) -> str:
    text = clip_signal_text(value, 180)
    if not text:
        return ""
    return hashlib.sha256(text.encode("utf-8", errors="replace")).hexdigest()[:24]


def _bounded_ref_hashes(values: object, *, limit: int = MAX_AFFINITY_REF_HASHES) -> tuple[str, ...]:
    return _merge_ref_hashes((), _list(values), limit=limit)


def _merge_ref_hashes(
    current: Iterable[object],
    incoming: Iterable[object],
    *,
    limit: int = MAX_AFFINITY_REF_HASHES,
) -> tuple[str, ...]:
    out: list[str] = []
    for value in (*tuple(current or ()), *tuple(incoming or ())):
        text = clip_signal_text(value, 180)
        digest = text if len(text) == 24 and all(char in "0123456789abcdef" for char in text) else _ref_hash(text)
        if digest and digest not in out:
            out.append(digest)
    return tuple(out[-max(1, int(limit or 1)):])


def _merge_refs(current: Iterable[object], incoming: Iterable[object], *, limit: int) -> tuple[str, ...]:
    out: list[str] = []
    for value in (*tuple(current or ()), *tuple(incoming or ())):
        text = clip_signal_text(value, 180)
        if not text or contains_sensitive_signal_text(text):
            continue
        if "\n" in text or "\r" in text or "\t" in text:
            continue
        if text not in out:
            out.append(text)
    return tuple(out[-max(1, int(limit or 1)):])


def _bounded_warnings(values: Iterable[object]) -> tuple[str, ...]:
    out: list[str] = []
    for value in values:
        text = clip_signal_text(value, 180)
        if text and text not in out:
            out.append(text)
        if len(out) >= MAX_AFFINITY_WARNINGS:
            break
    return tuple(out)


def _filter_values(value: object, allowed: frozenset[str]) -> set[str]:
    values = {str(item).strip().lower() for item in str(value or "").split(",") if str(item).strip()}
    return {item for item in values if item in allowed}


def _field(value: Any, name: str) -> object:
    if isinstance(value, Mapping):
        return value.get(name)
    return getattr(value, name, "")


def _list(value: object) -> tuple[object, ...]:
    if value is None:
        return ()
    if isinstance(value, tuple):
        return value
    if isinstance(value, list):
        return tuple(value)
    if isinstance(value, set):
        return tuple(value)
    return (value,)


def _metadata_sequence(value: object) -> tuple[object, ...]:
    if isinstance(value, (list, tuple, set)):
        return tuple(value)
    text = str(value or "").strip()
    if not text:
        return ()
    if text.startswith("[") and text.endswith("]"):
        try:
            parsed = ast.literal_eval(text)
        except (SyntaxError, ValueError):
            parsed = None
        if isinstance(parsed, (list, tuple, set)):
            return tuple(parsed)
    return (text,)


def _unit_float_or_none(value: object) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(number) or number < 0.0 or number > 1.0:
        return None
    return round(number, 6)


def _unit_float(value: object) -> float:
    if isinstance(value, bool):
        return 0.0
    try:
        number = float(value)
    except (TypeError, ValueError):
        return 0.0
    if not math.isfinite(number):
        return 0.0
    return round(max(0.0, min(1.0, number)), 6)


def _parse_ts(value: object) -> datetime:
    text = str(value or "").strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return datetime.now(timezone.utc)
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _event_file_stats(path: Path, *, max_bytes: int) -> dict[str, object]:
    try:
        if not path.is_file():
            return {"events": 0, "bytes": 0, "readable": True, "warning": ""}
        event_bytes = path.stat().st_size
        if event_bytes > max(0, int(max_bytes or 0)):
            return {
                "events": 0,
                "bytes": event_bytes,
                "readable": True,
                "warning": "affinity_events_too_large",
            }
        event_count = len(path.read_text(encoding="utf-8").splitlines())
    except (OSError, UnicodeDecodeError):
        return {"events": 0, "bytes": 0, "readable": False, "warning": "affinity_events_unreadable"}
    return {"events": event_count, "bytes": event_bytes, "readable": True, "warning": ""}


def _compact_payload(
    ok: bool,
    compacted: bool,
    before: Mapping[str, object],
    after: Mapping[str, object],
    warnings: Iterable[str],
) -> dict[str, object]:
    return {
        "ok": ok,
        "compacted": compacted,
        "events_before": int(before.get("events") or 0),
        "events_after": int(after.get("events") or 0),
        "bytes_before": int(before.get("bytes") or 0),
        "bytes_after": int(after.get("bytes") or 0),
        "warnings": list(_bounded_warnings(warnings)),
    }


def _json_line(value: dict[str, object]) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True) + "\n"


__all__ = [
    "AFFINITY_SCHEMA_VERSION",
    "AffinityEdge",
    "AffinityHint",
    "AffinityNode",
    "GhostAffinityStore",
    "GhostAffinitySyncResult",
    "apply_affinity_research_boost",
    "apply_affinity_work_boost",
]
