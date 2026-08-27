"""Bounded local Hebbian state for accepted Ghost memory candidates."""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, timezone
import hashlib
import json
import math
import os
from pathlib import Path
import uuid
from typing import Iterable

from codey.ghost.inbox import GhostInboxStore, GhostMemoryCandidate
from codey.ghost.numbers import coerce_unit_float
from codey.ghost.schema import SIGNAL_KINDS, SIGNAL_SCOPES, clip_signal_text
from codey.storage.file_lock import reset_event_backed_state, with_file_lock
from codey.storage.local_store import DEFAULT_STATE_HOME, delete_file, read_json, write_json_atomic


HEBBIAN_SCHEMA_VERSION = 1
MAX_GHOST_NODES = 500
MAX_GHOST_EDGES = 2_000
MAX_HEBBIAN_EVENTS = 5_000
MAX_HEBBIAN_STATE_BYTES = 4 * 1024 * 1024
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
MAX_HEBBIAN_WARNINGS = 20

NODE_KINDS = SIGNAL_KINDS
NODE_STATUSES = ("active", "superseded", "expired")
EDGE_RELATIONS = ("coactivated_with",)
_STATE_KIND = "ghost_hebbian_state_projection"


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
    def from_payload(cls, payload: object) -> "GhostNode | None":
        if not isinstance(payload, dict):
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
    def from_payload(cls, payload: object) -> "GhostEdge | None":
        if not isinstance(payload, dict):
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
        self.directory = Path(state_home) / "ghost"
        self.state_path = self.directory / "state.json"
        self.events_path = self.directory / "hebbian_events.jsonl"
        self.last_warnings: tuple[str, ...] = ()
        self._events_read_blocked = False

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
            with with_file_lock(self.events_path):
                nodes, edges = self._load_state()
                if self._events_read_blocked:
                    return GhostReinforceResult(False, "events_read_blocked")
                node_by_id = {node.id: node for node in nodes}
                edge_by_key = {_edge_key(edge): edge for edge in edges}
                now = _now()
                node_id = node_id_for_candidate(candidate)
                current = node_by_id.get(node_id)
                known_evidence = set(current.evidence_refs if current else ())
                new_evidence_refs = tuple(ref for ref in candidate.evidence_refs if ref not in known_evidence)
                if not new_evidence_refs:
                    changed_nodes: list[GhostNode] = []
                    if _candidate_is_manual_accept(candidate) and current is not None:
                        changed_nodes.extend(_supersede_conflicting_nodes(node_by_id, current, now=now))
                    changed_edges = _reinforce_coactivation_edges(
                        edge_by_key,
                        node_by_id,
                        current,
                        candidate,
                        related_candidates,
                        reward=reward,
                        now=now,
                    ) if current is not None else []
                    if not changed_nodes and not changed_edges:
                        return GhostReinforceResult(False, "duplicate_evidence")
                    nodes = _bounded_nodes(node_by_id.values())
                    edges = _bounded_edges(edge_by_key.values(), node_ids={node.id for node in nodes})
                    events = [
                        _node_event(node, action="superseded")
                        for node in changed_nodes
                    ]
                    events.extend(_edge_event(edge, action="reinforced") for edge in changed_edges)
                    if not self._append_events(events):
                        return GhostReinforceResult(False, "event_write_failed")
                    try:
                        self._write_projection(nodes, edges)
                    except (OSError, TypeError, ValueError):
                        delete_file(self.state_path)
                    self._compact_if_needed(nodes, edges)
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
                nodes = _bounded_nodes(node_by_id.values())
                edges = _bounded_edges(edge_by_key.values(), node_ids={node.id for node in nodes})
                events = [
                    _node_event(changed_node, action="superseded" if changed_node.status == "superseded" else "reinforced")
                    for changed_node in changed_nodes
                ]
                events.extend(_edge_event(edge, action="reinforced") for edge in changed_edges)
                if not self._append_events(events):
                    return GhostReinforceResult(False, "event_write_failed")
                try:
                    self._write_projection(nodes, edges)
                except (OSError, TypeError, ValueError):
                    delete_file(self.state_path)
                self._compact_if_needed(nodes, edges)
                return GhostReinforceResult(True, "reinforced", node=node, edges=tuple(changed_edges))
        except Exception:
            return GhostReinforceResult(False, "store_error")

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
            removed_count = int(removed.get("nodes", 0)) + int(removed.get("edges", 0))
            results.append(GhostReinforceResult(
                removed_count > 0,
                f"removed_{row.status}_candidate" if removed_count else f"no_{row.status}_state",
            ))
        for row in accepted_rows:
            related = [item for item in by_run.get(row.run_id or row.id, []) if item.id != row.id]
            results.append(self.reinforce_candidate(row, related_candidates=related))
        return tuple(results)

    def remove_candidate(self, candidate: GhostMemoryCandidate) -> dict[str, int]:
        with with_file_lock(self.events_path):
            nodes, edges = self._load_state()
            if self._events_read_blocked:
                raise OSError("hebbian events are unreadable")
            candidate_node_id = node_id_for_candidate(candidate)
            removed_ids = {
                node.id for node in nodes
                if node.id == candidate_node_id or candidate.id in node.candidate_ids
            }
            if not removed_ids:
                return {"nodes": 0, "edges": 0}
            remaining_nodes = [node for node in nodes if node.id not in removed_ids]
            removed_edges = [
                edge for edge in edges
                if edge.source in removed_ids or edge.target in removed_ids
            ]
            remaining_edges = [
                edge for edge in edges
                if edge.source not in removed_ids and edge.target not in removed_ids
            ]
            self._rewrite_events_from_state(
                remaining_nodes,
                remaining_edges,
                control_event=_control_event(
                    "ghost_hebbian_candidate_removed",
                    {
                        "candidate_id": candidate.id,
                        "removed_nodes": len(removed_ids),
                        "removed_edges": len(removed_edges),
                    },
                ),
            )
            # Symmetric with the reinforce path: a projection write failure
            # invalidates the derived projection, not the authoritative events.
            try:
                self._write_projection(remaining_nodes, remaining_edges)
            except (OSError, TypeError, ValueError):
                delete_file(self.state_path)
            return {"nodes": len(removed_ids), "edges": len(removed_edges)}

    def list_nodes(
        self,
        *,
        status: str | Iterable[str] | None = None,
        scope: str = "",
        project: str = "",
        session_id: str = "",
    ) -> tuple[GhostNode, ...]:
        try:
            nodes, _edges = self._load_state()
        except Exception:
            return ()
        statuses = _status_filter(status, allowed=NODE_STATUSES)
        normalized_scope = str(scope or "").strip().lower()
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
        return tuple(sorted(rows, key=lambda item: (item.weight, item.updated_at), reverse=True))

    def list_edges(self, *, relation: str = "") -> tuple[GhostEdge, ...]:
        try:
            _nodes, edges = self._load_state()
        except Exception:
            return ()
        normalized_relation = str(relation or "").strip().lower()
        rows = [
            edge for edge in edges
            if not normalized_relation or edge.relation == normalized_relation
        ]
        return tuple(sorted(rows, key=lambda item: (item.weight, item.updated_at), reverse=True))

    def export_state(self) -> dict[str, object]:
        nodes, edges = self._load_state()
        return {
            "schema_version": HEBBIAN_SCHEMA_VERSION,
            "state": self._projection_payload(nodes, edges),
            "events": list(self._read_events()),
            "warnings": list(self.last_warnings),
        }

    def reset_all(self) -> bool:
        try:
            reset_event_backed_state(self.events_path, self.state_path)
            self.last_warnings = ()
            self._events_read_blocked = False
            return True
        except OSError:
            return False

    def delete_scope(
        self,
        scope: str,
        *,
        project: str = "",
        session_id: str = "",
    ) -> dict[str, int]:
        normalized_scope = str(scope or "").strip().lower()
        if normalized_scope not in SIGNAL_SCOPES:
            raise ValueError("scope must be user, project, or session")
        scope_ref = _scope_ref_for_filter(normalized_scope, project=project, session_id=session_id)
        if normalized_scope in {"project", "session"} and not scope_ref:
            raise ValueError(f"{normalized_scope} reference is required")
        with with_file_lock(self.events_path):
            nodes, edges = self._load_state()
            removed_ids = {
                node.id for node in nodes
                if node.scope == normalized_scope and (normalized_scope == "user" or node.scope_ref == scope_ref)
            }
            if not removed_ids:
                return {"nodes": 0, "edges": 0}
            remaining_nodes = [node for node in nodes if node.id not in removed_ids]
            removed_edges = [
                edge for edge in edges
                if edge.source in removed_ids or edge.target in removed_ids
            ]
            remaining_edges = [
                edge for edge in edges
                if edge.source not in removed_ids and edge.target not in removed_ids
            ]
            self._rewrite_events_from_state(
                remaining_nodes,
                remaining_edges,
                control_event=_control_event(
                    "ghost_hebbian_scope_deleted",
                    {
                        "scope": normalized_scope,
                        "scope_ref": scope_ref,
                        "removed_nodes": len(removed_ids),
                        "removed_edges": len(removed_edges),
                    },
                ),
            )
            self._write_projection(remaining_nodes, remaining_edges)
            return {"nodes": len(removed_ids), "edges": len(removed_edges)}

    def rebuild_from_events(self) -> bool:
        try:
            with with_file_lock(self.events_path):
                nodes, edges = self._rebuild_state_from_events()
                if nodes is None or edges is None:
                    return False
                self._write_projection(nodes, edges)
                return True
        except Exception:
            return False

    def decay(self, *, min_interval_seconds: int = 0) -> dict[str, object]:
        with with_file_lock(self.events_path):
            if self.events_path.exists():
                self._read_events()
                if self._events_read_blocked:
                    return {
                        "removed_nodes": 0,
                        "removed_edges": 0,
                        "decayed_nodes": 0,
                        "decayed_edges": 0,
                        "skipped_reason": "events_read_blocked",
                        "warnings": list(self.last_warnings),
                    }
            nodes, edges = self._load_state()
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
            removed_nodes = len(nodes) - len(bounded_nodes)
            removed_edges = len(edges) - len(bounded_edges)
            decayed_node_count = sum(
                1 for before, after in zip(nodes, decayed_nodes, strict=False)
                if before.weight != after.weight or before.status != after.status
            )
            decayed_edge_count = sum(
                1 for before, after in zip(edges, decayed_edges, strict=False)
                if before.weight != after.weight
            )
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
                    "ghost_hebbian_state_decayed",
                    {
                        "removed_nodes": removed_nodes,
                        "removed_edges": removed_edges,
                        "decayed_nodes": decayed_node_count,
                        "decayed_edges": decayed_edge_count,
                    },
                ),
            )
            self._write_projection(bounded_nodes, bounded_edges)
            return {
                "removed_nodes": removed_nodes,
                "removed_edges": removed_edges,
                "decayed_nodes": decayed_node_count,
                "decayed_edges": decayed_edge_count,
                "skipped_reason": "",
            }

    def compact_if_needed(self) -> dict[str, object]:
        before = _event_file_stats(self.events_path, max_bytes=MAX_HEBBIAN_EVENTS_BYTES)
        if not before["readable"]:
            warning = str(before["warning"] or "hebbian_events_unreadable")
            self.last_warnings = (warning,)
            return {
                "ok": False,
                "compacted": False,
                "events_before": before["events"],
                "events_after": before["events"],
                "bytes_before": before["bytes"],
                "bytes_after": before["bytes"],
                "warnings": [warning],
            }
        if before["events"] <= MAX_HEBBIAN_EVENTS and before["bytes"] <= MAX_HEBBIAN_EVENTS_BYTES:
            return {
                "ok": True,
                "compacted": False,
                "events_before": before["events"],
                "events_after": before["events"],
                "bytes_before": before["bytes"],
                "bytes_after": before["bytes"],
                "warnings": list(self.last_warnings),
            }
        with with_file_lock(self.events_path):
            nodes, edges = self._load_state()
            if self._events_read_blocked:
                return {
                    "ok": False,
                    "compacted": False,
                    "events_before": before["events"],
                    "events_after": before["events"],
                    "bytes_before": before["bytes"],
                    "bytes_after": before["bytes"],
                    "warnings": list(self.last_warnings),
                }
            self._compact_if_needed(nodes, edges)
        after = _event_file_stats(self.events_path, max_bytes=MAX_HEBBIAN_EVENTS_BYTES)
        return {
            "ok": True,
            "compacted": after != before,
            "events_before": before["events"],
            "events_after": after["events"],
            "bytes_before": before["bytes"],
            "bytes_after": after["bytes"],
            "warnings": list(self.last_warnings),
        }

    def _load_state(self) -> tuple[list[GhostNode], list[GhostEdge]]:
        self._events_read_blocked = False
        payload = self._read_projection_payload()
        if payload is not None:
            nodes, edges = self._rows_from_projection(payload)
            if nodes is not None and edges is not None:
                return nodes, edges
        rebuilt_nodes, rebuilt_edges = self._rebuild_state_from_events()
        if rebuilt_nodes is None or rebuilt_edges is None:
            return [], []
        try:
            self._write_projection(rebuilt_nodes, rebuilt_edges)
        except (OSError, TypeError, ValueError):
            pass
        return rebuilt_nodes, rebuilt_edges

    def _read_projection_payload(self) -> dict[str, object] | None:
        if not self.state_path.exists():
            return None
        payload = read_json(self.state_path, max_bytes=MAX_HEBBIAN_STATE_BYTES)
        if not isinstance(payload, dict):
            self._quarantine(self.state_path)
            return None
        if payload.get("schema_version") != HEBBIAN_SCHEMA_VERSION:
            self._quarantine(self.state_path)
            return None
        if payload.get("kind") != _STATE_KIND:
            self._quarantine(self.state_path)
            return None
        return payload

    def _rows_from_projection(
        self,
        payload: dict[str, object],
    ) -> tuple[list[GhostNode] | None, list[GhostEdge] | None]:
        raw_nodes = payload.get("nodes")
        raw_edges = payload.get("edges")
        if not isinstance(raw_nodes, list) or not isinstance(raw_edges, list):
            return None, None
        nodes = [
            node for node in (GhostNode.from_payload(item) for item in raw_nodes[:MAX_GHOST_NODES])
            if node is not None
        ]
        node_ids = {node.id for node in nodes}
        edges = [
            edge for edge in (GhostEdge.from_payload(item) for item in raw_edges[:MAX_GHOST_EDGES])
            if edge is not None and edge.source in node_ids and edge.target in node_ids
        ]
        return _bounded_nodes(nodes), _bounded_edges(edges, node_ids=node_ids)

    def _rebuild_state_from_events(self) -> tuple[list[GhostNode] | None, list[GhostEdge] | None]:
        events = self._read_events()
        if self._events_read_blocked:
            return None, None
        nodes: dict[str, GhostNode] = {}
        edges: dict[tuple[str, str, str], GhostEdge] = {}
        for event in events:
            event_type = str(event.get("type") or "")
            if event_type in {"ghost_hebbian_node_upsert", "ghost_hebbian_node_superseded"}:
                node = GhostNode.from_payload(event.get("node"))
                if node is not None:
                    nodes[node.id] = node
            elif event_type == "ghost_hebbian_edge_upsert":
                edge = GhostEdge.from_payload(event.get("edge"))
                if edge is not None:
                    edges[_edge_key(edge)] = edge
        bounded_nodes = _bounded_nodes(nodes.values())
        bounded_edges = _bounded_edges(edges.values(), node_ids={node.id for node in bounded_nodes})
        return bounded_nodes, bounded_edges

    def _read_events(self) -> tuple[dict[str, object], ...]:
        warnings: list[str] = []
        self._events_read_blocked = False
        try:
            if not self.events_path.is_file():
                self.last_warnings = ()
                return ()
            if self.events_path.stat().st_size > MAX_HEBBIAN_EVENTS_BYTES:
                self.last_warnings = ("hebbian_events_too_large",)
                self._events_read_blocked = True
                return ()
            lines = self.events_path.read_text(encoding="utf-8").splitlines()
        except (OSError, UnicodeDecodeError):
            self.last_warnings = ("hebbian_events_unreadable",)
            self._events_read_blocked = True
            return ()
        events: list[dict[str, object]] = []
        for index, line in enumerate(lines, start=1):
            if not line.strip():
                continue
            try:
                value = json.loads(line)
            except json.JSONDecodeError:
                warnings.append(f"hebbian_events.jsonl:{index}:bad_json")
                continue
            if not isinstance(value, dict):
                warnings.append(f"hebbian_events.jsonl:{index}:not_object")
                continue
            if value.get("schema_version") != HEBBIAN_SCHEMA_VERSION:
                warnings.append(f"hebbian_events.jsonl:{index}:unsupported_schema")
                continue
            events.append(value)
        self.last_warnings = tuple(warnings[:MAX_HEBBIAN_WARNINGS])
        return tuple(events)

    def _write_projection(self, nodes: Iterable[GhostNode], edges: Iterable[GhostEdge]) -> None:
        node_rows = _bounded_nodes(nodes)
        edge_rows = _bounded_edges(edges, node_ids={node.id for node in node_rows})
        payload = self._projection_payload(node_rows, edge_rows)
        write_json_atomic(self.state_path, payload, max_bytes=MAX_HEBBIAN_STATE_BYTES)

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
            "source": "hebbian_events.jsonl",
            "updated_at": _now(),
            "nodes": [node.to_payload() for node in node_rows],
            "edges": [edge.to_payload() for edge in edge_rows],
            "warnings": list(self.last_warnings),
        }

    def _append_events(self, events: Iterable[dict[str, object]]) -> bool:
        rows = list(events)
        if not rows:
            return True
        try:
            self.directory.mkdir(parents=True, exist_ok=True)
            with self.events_path.open("a", encoding="utf-8", newline="\n") as handle:
                for event in rows:
                    handle.write(_json_line(event))
            return True
        except (OSError, TypeError, ValueError):
            return False

    def _rewrite_events_from_state(
        self,
        nodes: Iterable[GhostNode],
        edges: Iterable[GhostEdge],
        *,
        control_event: dict[str, object] | None = None,
    ) -> None:
        node_rows = _bounded_nodes(nodes)
        events = [_node_event(node, action="compacted") for node in node_rows]
        node_ids = {node.id for node in node_rows}
        events.extend(
            _edge_event(edge, action="compacted")
            for edge in _bounded_edges(edges, node_ids=node_ids)
        )
        if control_event is not None:
            events.append(control_event)
        self._write_events_atomic(events)

    def _write_events_atomic(self, events: Iterable[dict[str, object]]) -> None:
        rows = list(events)
        data = "".join(_json_line(event) for event in rows).encode("utf-8")
        if len(data) > MAX_HEBBIAN_EVENTS_BYTES:
            raise ValueError("ghost hebbian events are too large")
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

    def _compact_if_needed(
        self,
        nodes: Iterable[GhostNode],
        edges: Iterable[GhostEdge],
    ) -> None:
        try:
            event_bytes = self.events_path.stat().st_size
            if event_bytes > MAX_HEBBIAN_EVENTS_BYTES:
                line_count = MAX_HEBBIAN_EVENTS + 1
            else:
                line_count = len(self.events_path.read_text(encoding="utf-8").splitlines())
        except (OSError, UnicodeDecodeError):
            return
        if line_count <= MAX_HEBBIAN_EVENTS and event_bytes <= MAX_HEBBIAN_EVENTS_BYTES:
            return
        reason = "event_bytes_limit" if event_bytes > MAX_HEBBIAN_EVENTS_BYTES else "event_count_limit"
        try:
            self._rewrite_events_from_state(
                nodes,
                edges,
                control_event=_control_event(
                    "ghost_hebbian_store_compacted",
                    {
                        "reason": reason,
                        "max_events": MAX_HEBBIAN_EVENTS,
                        "max_event_bytes": MAX_HEBBIAN_EVENTS_BYTES,
                    },
                ),
            )
        except (OSError, TypeError, ValueError):
            pass

    def _quarantine(self, path: Path) -> None:
        try:
            target = path.with_name(f"{path.name}.quarantine.{_compact_timestamp()}")
            path.replace(target)
        except OSError:
            pass


def node_id_for_candidate(candidate: GhostMemoryCandidate) -> str:
    raw = "|".join((
        candidate.scope,
        _scope_ref_for_candidate(candidate),
        candidate.signal_kind,
        candidate.conflict_key,
        candidate.value_key,
    ))
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
    return row.last_decayed_at or row.last_reinforced_at or row.updated_at


def _decayed_weight(weight: float, last_reinforced_at: str, now: str, half_life_days: float) -> float:
    age = max(0.0, (_parse_ts(now) - _parse_ts(last_reinforced_at)).total_seconds())
    half_life_seconds = max(1.0, float(half_life_days) * 24.0 * 60.0 * 60.0)
    decay_rate = math.log(2.0) / half_life_seconds
    return _clamp01(float(weight or 0.0) * math.exp(-decay_rate * age))


def _any_decay_due(
    rows: Iterable[GhostNode | GhostEdge],
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


def _bounded_nodes(nodes: Iterable[GhostNode]) -> list[GhostNode]:
    rows = [node for node in nodes if node.weight >= MIN_NODE_WEIGHT or node.status != "active"]
    rows.sort(key=lambda item: (item.status == "active", item.weight, item.updated_at), reverse=True)
    return rows[:MAX_GHOST_NODES]


def _bounded_edges(edges: Iterable[GhostEdge], *, node_ids: set[str]) -> list[GhostEdge]:
    rows = [
        edge for edge in edges
        if edge.source in node_ids and edge.target in node_ids and edge.weight >= MIN_EDGE_WEIGHT
    ]
    rows.sort(key=lambda item: (item.weight, item.updated_at), reverse=True)
    bounded: list[GhostEdge] = []
    degree: dict[str, int] = {}
    for edge in rows:
        if len(bounded) >= MAX_GHOST_EDGES:
            break
        if degree.get(edge.source, 0) >= MAX_EDGE_OUT_DEGREE:
            continue
        if degree.get(edge.target, 0) >= MAX_EDGE_OUT_DEGREE:
            continue
        bounded.append(edge)
        degree[edge.source] = degree.get(edge.source, 0) + 1
        degree[edge.target] = degree.get(edge.target, 0) + 1
    return bounded


def _node_event(node: GhostNode, *, action: str) -> dict[str, object]:
    event_type = "ghost_hebbian_node_superseded" if node.status == "superseded" else "ghost_hebbian_node_upsert"
    return {
        "schema_version": HEBBIAN_SCHEMA_VERSION,
        "type": event_type,
        "ts": _now(),
        "action": action,
        "node": node.to_payload(),
    }


def _edge_event(edge: GhostEdge, *, action: str) -> dict[str, object]:
    return {
        "schema_version": HEBBIAN_SCHEMA_VERSION,
        "type": "ghost_hebbian_edge_upsert",
        "ts": _now(),
        "action": action,
        "edge": edge.to_payload(),
    }


def _control_event(event_type: str, payload: dict[str, object]) -> dict[str, object]:
    return {
        "schema_version": HEBBIAN_SCHEMA_VERSION,
        "type": event_type,
        "ts": _now(),
        "payload": payload,
    }


def _candidate_has_provenance(candidate: GhostMemoryCandidate) -> bool:
    if not candidate.scope or candidate.scope not in SIGNAL_SCOPES:
        return False
    if not candidate.session_id and not candidate.run_id:
        return False
    if candidate.scope == "project" and not candidate.project:
        return False
    if candidate.scope == "session" and not candidate.session_id:
        return False
    return True


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
    if candidate.scope == "project":
        return candidate.project
    if candidate.scope == "session":
        return candidate.session_id
    return ""


def _scope_ref_for_filter(scope: str, *, project: str, session_id: str) -> str:
    if scope == "project":
        return _normalize_project(project)
    if scope == "session":
        return clip_signal_text(session_id, 120)
    return ""


def _normalize_project(value: object) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    try:
        return clip_signal_text(Path(text).expanduser().resolve(), 240)
    except (OSError, RuntimeError, ValueError):
        return clip_signal_text(text, 240)


def _edge_key(edge: GhostEdge) -> tuple[str, str, str]:
    return _canonical_edge_key(edge.source, edge.target, edge.relation)


def _canonical_edge_key(source: str, target: str, relation: str) -> tuple[str, str, str]:
    a, b = sorted((str(source), str(target)))
    return a, b, str(relation or "coactivated_with")


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
    return tuple(out[-max(1, int(limit or 1)):])


def _clean_refs(value: object, *, limit: int) -> tuple[str, ...]:
    if not isinstance(value, (list, tuple)):
        return ()
    return _merge_refs((), (str(item) for item in value), limit=limit)


def _status_filter(value: str | Iterable[str] | None, *, allowed: tuple[str, ...]) -> set[str]:
    if value is None:
        return set()
    if isinstance(value, str):
        raw_values = value.split(",")
    else:
        raw_values = list(value)
    return {
        str(item).strip().lower()
        for item in raw_values
        if str(item).strip().lower() in allowed
    }


def _coerce_weight(value: object) -> float | None:
    return coerce_unit_float(value, digits=6)


def _coerce_confidence(value: object) -> float | None:
    return coerce_unit_float(value, digits=4)


def _coerce_reward(value: object) -> float:
    try:
        reward = float(value)
    except (TypeError, ValueError):
        return 1.0
    if not math.isfinite(reward):
        return 1.0
    return max(0.0, min(1.0, reward))


def _clamp01(value: float) -> float:
    return round(max(0.0, min(1.0, float(value or 0.0))), 6)


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


def _compact_timestamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


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
                "warning": "hebbian_events_too_large",
            }
        event_count = len(path.read_text(encoding="utf-8").splitlines())
    except (OSError, UnicodeDecodeError):
        return {"events": 0, "bytes": 0, "readable": False, "warning": "hebbian_events_unreadable"}
    return {"events": event_count, "bytes": event_bytes, "readable": True, "warning": ""}


def _json_line(value: dict[str, object]) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ) + "\n"
