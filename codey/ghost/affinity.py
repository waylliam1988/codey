"""Bounded local Affinity Index for audited Ghost facts.

Affinity is a deterministic association ledger. It is not evidence, not a
permission system, and not an execution policy.

Store-only module. Data shapes, identity, cleaning, and spec payloads live
in :mod:`codey.ghost.affinity_model`; source conversions live in
:mod:`codey.ghost.affinity_sources`; event construction, validation, and
the single ``rows <- events`` transition live in
:mod:`codey.ghost.affinity_events`. This module owns persistence,
transactions, queries, and hints, and delegates to those owners.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, is_dataclass, replace
from pathlib import Path
from typing import Any

from codey.ghost import _common
from codey.ghost.affinity_events import (
    _any_decay_due,
    _bounded_edges,
    _bounded_nodes,
    _decay_applied_event,
    _decay_edge,
    _decay_node,
    _delete_scope_rows,
    _edge_reinforced_event,
    _node_reinforced_event,
    _projection_payload,
    _reinforce_edge,
    _reinforce_node,
    _scope_deleted_event,
    _snapshot_event,
    replay_affinity_events,
)
from codey.ghost.affinity_model import (
    AFFINITY_EDGE_RELATIONS,
    AFFINITY_EDGE_STATUSES,
    AFFINITY_NODE_KINDS,
    AFFINITY_NODE_STATUSES,
    AFFINITY_SCHEMA_VERSION,
    MAX_AFFINITY_EVENTS,
    MAX_AFFINITY_EVENTS_BYTES,
    MAX_AFFINITY_HINT_REFS,
    MAX_HINTS,
    AffinityEdge,
    AffinityHint,
    AffinityNode,
    GhostAffinitySyncResult,
    _bounded_refs,
    _bounded_warnings,
    _clean_hint_kind,
    _clean_key,
    _clean_scope,
    _edge_id,
    _field,
    _node_id,
    _unit_float,
)
from codey.ghost.affinity_sources import (
    _concepts_from_candidate,
    _concepts_from_work_item,
    _scope_from_source,
    _scope_ref,
    collect_source_specs,
)
from codey.ghost.events import (
    compact_result_payload as _compact_payload,
)
from codey.ghost.events import (
    over_compact_budget,
)
from codey.ghost.graph_store import GhostGraph
from codey.ghost.schema import clip_signal_text
from codey.ghost.sqlite import GhostSQLite
from codey.runtime.core import cancellation
from codey.storage.local_store import (
    DEFAULT_STATE_HOME,
)


@dataclass(frozen=True)
class _AffinityMutation:
    result: object
    append_events: tuple[dict[str, object], ...] = ()
    replace_events: tuple[dict[str, object], ...] | None = None
    nodes: tuple[AffinityNode, ...] = ()
    edges: tuple[AffinityEdge, ...] = ()
    write_state: bool = True
    compact: bool = True


class GhostAffinityStore:
    def __init__(self, state_home: str | Path = DEFAULT_STATE_HOME) -> None:
        self.database = GhostSQLite(state_home)
        self.graph = GhostGraph(self.database, "affinity")
        self.last_warnings: tuple[str, ...] = ()

    def sync_from_sources(
        self,
        *,
        hebbian_store: Any = None,
        work_queue_store: Any = None,
        research_interest_candidates: Iterable[Any] = (),
        run_projection: Any = None,
        terminal_event: Mapping[str, object] | None = None,
        session_id: str = "",
        project: str = "",
    ) -> GhostAffinitySyncResult:
        self.last_warnings = ()
        try:
            node_specs, edge_specs = collect_source_specs(
                hebbian_store=hebbian_store,
                work_queue_store=work_queue_store,
                research_interest_candidates=research_interest_candidates,
                run_projection=run_projection,
                terminal_event=terminal_event,
                session_id=session_id,
                project=project,
            )

            def decide(events: list[dict[str, object]]) -> _AffinityMutation:
                nodes, edges = replay_affinity_events(events)
                node_by_id = {node.id: node for node in nodes}
                edge_by_id = {edge.id: edge for edge in edges}
                now = _common.now_iso_z()
                append_events: list[dict[str, object]] = []
                changed_nodes = 0
                for node_spec in node_specs:
                    node, changed = _reinforce_node(
                        node_by_id.get(_node_id(node_spec.kind, node_spec.scope, node_spec.scope_ref, node_spec.key)),
                        node_spec,
                        now=now,
                    )
                    if not changed:
                        continue
                    node_by_id[node.id] = node
                    append_events.append(_node_reinforced_event(node_spec, ts=now))
                    changed_nodes += 1
                changed_edges = 0
                for edge_spec in edge_specs:
                    if edge_spec.source not in node_by_id or edge_spec.target not in node_by_id:
                        continue
                    edge, changed = _reinforce_edge(
                        edge_by_id.get(
                            _edge_id(
                                edge_spec.source,
                                edge_spec.target,
                                edge_spec.relation,
                                edge_spec.scope,
                                edge_spec.scope_ref,
                            )
                        ),
                        edge_spec,
                        now=now,
                    )
                    if not changed:
                        continue
                    edge_by_id[edge.id] = edge
                    append_events.append(_edge_reinforced_event(edge_spec, ts=now))
                    changed_edges += 1
                bounded_nodes = _bounded_nodes(node_by_id.values())
                bounded_edges = _bounded_edges(edge_by_id.values(), node_ids={node.id for node in bounded_nodes})
                if not append_events:
                    self.last_warnings = ()
                    return _AffinityMutation(
                        GhostAffinitySyncResult(
                            True,
                            skipped_reason="no_change",
                            total_nodes=len(bounded_nodes),
                            total_edges=len(bounded_edges),
                        ),
                        nodes=tuple(bounded_nodes),
                        edges=tuple(bounded_edges),
                        write_state=False,
                        compact=False,
                    )
                new_nodes, new_edges = replay_affinity_events((*events, *append_events))
                return _AffinityMutation(
                    GhostAffinitySyncResult(
                        True,
                        nodes_changed=changed_nodes,
                        edges_changed=changed_edges,
                        total_nodes=len(new_nodes),
                        total_edges=len(new_edges),
                        warnings=self.last_warnings,
                    ),
                    append_events=tuple(append_events),
                    nodes=tuple(new_nodes),
                    edges=tuple(new_edges),
                )

            result = self._mutate(decide)
            if isinstance(result, GhostAffinitySyncResult):
                return result
            return self._sync_failed("affinity_error")
        except (OSError, TypeError, ValueError, sqlite3.Error) as exc:
            self.last_warnings = (f"affinity_store_error:{type(exc).__name__}",)
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
        with self.graph.transaction(write=False):
            return self._list_nodes_unlocked(
                kind=kind,
                status=status,
                scope=scope,
                project=project,
                session_id=session_id,
            )

    def _list_nodes_unlocked(
        self,
        *,
        kind: str = "",
        status: str = "",
        scope: str = "",
        project: str = "",
        session_id: str = "",
    ) -> tuple[AffinityNode, ...]:
        try:
            nodes, _edges = self._load_state_for_read_unlocked()
        except (cancellation.TaskCancelled, cancellation.DeadlineExceeded):
            raise
        except Exception:
            return ()
        kinds = _common.filter_values(kind, AFFINITY_NODE_KINDS)
        statuses = _common.filter_values(status, AFFINITY_NODE_STATUSES)
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
        return tuple(
            sorted(rows, key=lambda item: (item.status == "active", item.weight, item.updated_at), reverse=True)
        )

    def list_edges(
        self,
        *,
        relation: str = "",
        status: str = "",
        scope: str = "",
        project: str = "",
        session_id: str = "",
    ) -> tuple[AffinityEdge, ...]:
        with self.graph.transaction(write=False):
            return self._list_edges_unlocked(
                relation=relation,
                status=status,
                scope=scope,
                project=project,
                session_id=session_id,
            )

    def _list_edges_unlocked(
        self,
        *,
        relation: str = "",
        status: str = "",
        scope: str = "",
        project: str = "",
        session_id: str = "",
    ) -> tuple[AffinityEdge, ...]:
        try:
            _nodes, edges = self._load_state_for_read_unlocked()
        except (cancellation.TaskCancelled, cancellation.DeadlineExceeded):
            raise
        except Exception:
            return ()
        relations = _common.filter_values(relation, AFFINITY_EDGE_RELATIONS)
        statuses = _common.filter_values(status, AFFINITY_EDGE_STATUSES)
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
        return tuple(
            sorted(rows, key=lambda item: (item.status == "active", item.weight, item.updated_at), reverse=True)
        )

    def query_directive_order_hints(
        self,
        nodes: Iterable[Any],
        *,
        project: str = "",
        session_id: str = "",
    ) -> tuple[AffinityHint, ...]:
        with self.graph.transaction(write=False):
            return self._query_directive_order_hints_unlocked(nodes, project=project, session_id=session_id)

    def _query_directive_order_hints_unlocked(
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
        affinity_nodes, _edges = self._load_state_for_read_unlocked()
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
            hints.append(
                _hint(
                    "directive_order",
                    hebbian_id,
                    node.weight,
                    node.confidence,
                    "confirmed_memory_reinforced",
                    node.source_refs,
                )
            )
        return _bounded_hints(hints)

    def query_work_priority_hints(
        self,
        items: Iterable[Any],
        *,
        project: str = "",
        session_id: str = "",
    ) -> tuple[AffinityHint, ...]:
        with self.graph.transaction(write=False):
            return self._query_work_priority_hints_unlocked(items, project=project, session_id=session_id)

    def _query_work_priority_hints_unlocked(
        self,
        items: Iterable[Any],
        *,
        project: str = "",
        session_id: str = "",
    ) -> tuple[AffinityHint, ...]:
        nodes, edges = self._load_state_for_read_unlocked()
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
                hints.append(
                    _hint(
                        "work_priority",
                        item_id,
                        weight,
                        confidence or 0.5,
                        reason or "affinity_reinforced",
                        refs,
                    )
                )
        return _bounded_hints(hints)

    def query_research_priority_hints(
        self,
        candidates: Iterable[Any],
        *,
        project: str = "",
        session_id: str = "",
    ) -> tuple[AffinityHint, ...]:
        with self.graph.transaction(write=False):
            return self._query_research_priority_hints_unlocked(candidates, project=project, session_id=session_id)

    def _query_research_priority_hints_unlocked(
        self,
        candidates: Iterable[Any],
        *,
        project: str = "",
        session_id: str = "",
    ) -> tuple[AffinityHint, ...]:
        nodes, _edges = self._load_state_for_read_unlocked()
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
                hints.append(
                    _hint(
                        "research_priority",
                        candidate_id,
                        best_weight,
                        best_confidence or 0.5,
                        "research_concept_reinforced",
                        refs,
                    )
                )
        return _bounded_hints(hints)

    def export_state(self) -> dict[str, object]:
        with self.graph.transaction(write=False):
            nodes, edges = self._load_state_for_read_unlocked()
            return {
                "schema_version": AFFINITY_SCHEMA_VERSION,
                "affinity": _projection_payload(
                    nodes, edges, generated_at=_common.now_iso_z(), warnings=self.last_warnings
                ),
                "affinity_events": self.graph.events(),
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
    ) -> dict[str, object]:
        normalized_scope = _clean_scope(scope)
        if not normalized_scope:
            raise ValueError("scope must be user, project, or session")
        scope_ref = _scope_ref_for_filter(normalized_scope, project=project, session_id=session_id)
        if normalized_scope in {"project", "session"} and not scope_ref:
            raise ValueError(f"{normalized_scope} reference is required")

        def decide(events: list[dict[str, object]]) -> _AffinityMutation:
            nodes, edges = replay_affinity_events(events)
            kept_nodes, kept_edges, removed_nodes, removed_edges = _delete_scope_rows(
                nodes,
                edges,
                normalized_scope=normalized_scope,
                scope_ref=scope_ref,
            )
            if not removed_nodes and not removed_edges:
                return _AffinityMutation(
                    {"nodes": 0, "edges": 0, "warnings": []},
                    nodes=tuple(nodes),
                    edges=tuple(edges),
                    write_state=False,
                    compact=False,
                )
            event = _scope_deleted_event(
                normalized_scope,
                scope_ref if normalized_scope != "user" else "",
                removed_nodes=removed_nodes,
                removed_edges=removed_edges,
                ts=_common.now_iso_z(),
            )
            new_nodes, new_edges = replay_affinity_events((*events, event))
            return _AffinityMutation(
                {"nodes": removed_nodes, "edges": removed_edges, "warnings": []},
                append_events=(event,),
                nodes=tuple(new_nodes),
                edges=tuple(new_edges),
            )

        result = self._mutate(decide)
        return result if isinstance(result, dict) else {"nodes": 0, "edges": 0, "warnings": ["affinity_error"]}

    def rebuild_from_events(self) -> bool:
        with self.graph.transaction():
            nodes, edges = replay_affinity_events(self.graph.events())
            self._write_state(nodes, edges, warnings=())
        return True

    def decay(self, *, min_interval_seconds: int = 0) -> dict[str, object]:
        interval = max(0, (min_interval_seconds or 0))

        def decide(events: list[dict[str, object]]) -> _AffinityMutation:
            nodes, edges = replay_affinity_events(events)
            now = _common.now_iso_z()
            if interval and not _any_decay_due((*nodes, *edges), now=now, min_interval_seconds=interval):
                return _AffinityMutation(
                    {
                        "removed_nodes": 0,
                        "removed_edges": 0,
                        "decayed_nodes": 0,
                        "decayed_edges": 0,
                        "skipped_reason": "min_interval",
                    },
                    nodes=tuple(nodes),
                    edges=tuple(edges),
                    write_state=False,
                    compact=False,
                )
            decayed_nodes = [_decay_node(node, now=now) for node in nodes]
            decayed_edges = [_decay_edge(edge, now=now) for edge in edges]
            bounded_nodes = _bounded_nodes(decayed_nodes)
            bounded_edges = _bounded_edges(decayed_edges, node_ids={node.id for node in bounded_nodes})
            decayed_node_count = sum(
                1
                for before, after in zip(nodes, decayed_nodes, strict=False)
                if before.weight != after.weight or before.status != after.status
            )
            decayed_edge_count = sum(
                1
                for before, after in zip(edges, decayed_edges, strict=False)
                if before.weight != after.weight or before.status != after.status
            )
            removed_nodes = len(nodes) - len(bounded_nodes)
            removed_edges = len(edges) - len(bounded_edges)
            if not removed_nodes and not removed_edges and not decayed_node_count and not decayed_edge_count:
                return _AffinityMutation(
                    {
                        "removed_nodes": 0,
                        "removed_edges": 0,
                        "decayed_nodes": 0,
                        "decayed_edges": 0,
                        "skipped_reason": "no_change",
                        "warnings": [],
                    },
                    nodes=tuple(nodes),
                    edges=tuple(edges),
                    write_state=False,
                    compact=False,
                )
            event = _decay_applied_event(
                removed_nodes=removed_nodes,
                removed_edges=removed_edges,
                decayed_nodes=decayed_node_count,
                decayed_edges=decayed_edge_count,
                min_interval_seconds=interval,
                ts=now,
            )
            new_nodes, new_edges = replay_affinity_events((*events, event))
            return _AffinityMutation(
                {
                    "removed_nodes": removed_nodes,
                    "removed_edges": removed_edges,
                    "decayed_nodes": decayed_node_count,
                    "decayed_edges": decayed_edge_count,
                    "skipped_reason": "",
                    "warnings": [],
                },
                append_events=(event,),
                nodes=tuple(new_nodes),
                edges=tuple(new_edges),
            )

        result = self._mutate(decide)
        if not isinstance(result, dict):
            raise TypeError("invalid affinity decay result")
        return result

    def compact_if_needed(self) -> dict[str, object]:
        with self.graph.transaction():
            before = self.graph.event_stats()
            if not over_compact_budget(before, max_events=MAX_AFFINITY_EVENTS, max_bytes=MAX_AFFINITY_EVENTS_BYTES):
                return _compact_payload(True, False, before, before, ())
            nodes, edges = self._load_state_for_read_unlocked()
            self.graph.append(
                [_snapshot_event(nodes, edges, ts=_common.now_iso_z(), reason="events_compacted")], replace=True
            )
            self._write_state(nodes, edges, warnings=())
            after = self.graph.event_stats()
            return _compact_payload(True, True, before, after, ())

    def _load_state_for_read_unlocked(self) -> tuple[list[AffinityNode], list[AffinityEdge]]:
        raw_nodes, raw_edges = self.graph.rows()
        nodes = [AffinityNode.from_payload(row) for row in raw_nodes]
        edges = [AffinityEdge.from_payload(row) for row in raw_edges]
        if any(node is None for node in nodes) or any(edge is None for edge in edges):
            raise OSError("invalid Ghost affinity graph row")
        return [node for node in nodes if node is not None], [edge for edge in edges if edge is not None]

    def _mutate(self, decide: Any) -> object:
        with self.graph.transaction():
            self.graph.rows()
            self.last_warnings = ()
            mutation = decide(self.graph.events())
            if not isinstance(mutation, _AffinityMutation):
                raise TypeError("invalid affinity mutation result")
            if mutation.replace_events is not None:
                self.graph.append(mutation.replace_events, replace=True)
            elif mutation.append_events:
                self.graph.append(mutation.append_events)
            if mutation.write_state:
                self._write_state(mutation.nodes, mutation.edges, warnings=())
            if mutation.compact:
                self.compact_if_needed()
            result = mutation.result
            if is_dataclass(result) and not isinstance(result, type) and hasattr(result, "warnings"):
                return replace(result, warnings=self.last_warnings)
            if isinstance(result, dict):
                return dict(result, warnings=list(self.last_warnings))
            return result

    def _write_state(
        self, nodes: Iterable[AffinityNode], edges: Iterable[AffinityEdge], *, warnings: Iterable[str]
    ) -> None:
        self.graph.replace_rows((node.to_payload() for node in nodes), (edge.to_payload() for edge in edges))

    def _sync_failed(self, reason: str) -> GhostAffinitySyncResult:
        warnings = self.last_warnings or ((reason,) if reason else ())
        self.last_warnings = _bounded_warnings(warnings)
        return GhostAffinitySyncResult(False, skipped_reason=reason, warnings=self.last_warnings)


def apply_affinity_work_boost(priority: float, hints: Iterable[Any], target: str) -> float:
    base = _unit_float(priority)
    boost = _hint_boost(hints, target, maximum=0.12)
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


__all__ = [
    "AffinityEdge",
    "AffinityHint",
    "AffinityNode",
    "GhostAffinityStore",
    "GhostAffinitySyncResult",
    "apply_affinity_work_boost",
]
