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

import contextlib
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, is_dataclass, replace
from pathlib import Path
from typing import Any

from codey.ghost import _common
from codey.ghost._warnings import event_read_warnings
from codey.ghost.affinity_events import (
    _AFFINITY_EVENT_TYPES,
    _affinity_events_replay_cleanly,
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
    _valid_affinity_event,
    replay_affinity_events,
)
from codey.ghost.affinity_model import (
    _STATE_KIND,
    AFFINITY_EDGE_RELATIONS,
    AFFINITY_EDGE_STATUSES,
    AFFINITY_NODE_KINDS,
    AFFINITY_NODE_STATUSES,
    AFFINITY_SCHEMA_VERSION,
    MAX_AFFINITY_EVENTS,
    MAX_AFFINITY_EVENTS_BYTES,
    MAX_AFFINITY_HINT_REFS,
    MAX_AFFINITY_STATE_BYTES,
    MAX_AFFINITY_WARNINGS,
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
    _list,
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
from codey.ghost.event_log import (
    GhostEventLog,
)
from codey.ghost.event_log import (
    compact_result_payload as _compact_payload,
)
from codey.ghost.event_log import (
    event_file_stats as _event_file_stats,
)
from codey.ghost.event_projection import (
    over_compact_budget,
    read_projection_payload,
)
from codey.ghost.schema import clip_signal_text
from codey.runtime.core import cancellation
from codey.storage.event_state import reset_event_backed_state
from codey.storage.file_lock import with_file_lock
from codey.storage.local_store import (
    DEFAULT_STATE_HOME,
    backup_corrupt_file,
    delete_file,
    write_json_atomic,
)


@dataclass(frozen=True)
class _AffinityMutation:
    result: object
    append_events: tuple[dict[str, object], ...] = ()
    replace_events: tuple[dict[str, object], ...] | None = None
    nodes: tuple[AffinityNode, ...] = ()
    edges: tuple[AffinityEdge, ...] = ()
    write_projection: bool = True
    compact: bool = True


class GhostAffinityStore:
    def __init__(self, state_home: str | Path = DEFAULT_STATE_HOME) -> None:
        self.directory = Path(state_home) / "ghost"
        self.projection_path = self.directory / "affinity.json"
        self.events_path = self.directory / "affinity_events.jsonl"
        self.last_warnings: tuple[str, ...] = ()
        self._events_read_blocked = False
        self._events_blocked_reason = ""

    def _event_log(self) -> GhostEventLog:
        return GhostEventLog(
            self.events_path,
            schema_version=AFFINITY_SCHEMA_VERSION,
            max_bytes=MAX_AFFINITY_EVENTS_BYTES,
            max_warnings=MAX_AFFINITY_WARNINGS,
            source_name="affinity_events.jsonl",
            allowed_event_kinds=_AFFINITY_EVENT_TYPES,
            bad_row_policy="block",
            event_validator=lambda event: _valid_affinity_event(event),
        )

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
                        edge_by_id.get(_edge_id(edge_spec.source, edge_spec.target, edge_spec.relation, edge_spec.scope, edge_spec.scope_ref)),
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
                        write_projection=False,
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

            result = self._mutate_event_log(decide)
            if isinstance(result, GhostAffinitySyncResult):
                return result
            return self._sync_failed("affinity_error")
        except (OSError, TypeError, ValueError):
            if self._events_read_blocked:
                return self._sync_failed(self._events_blocked_reason or "events_read_blocked")
            if "affinity_event_write_failed" in self.last_warnings:
                return self._sync_failed("event_write_failed")
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
        with with_file_lock(self.events_path):
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
        with with_file_lock(self.events_path):
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
        with with_file_lock(self.events_path):
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
        affinity_nodes, _edges = self._load_state_for_hint_unlocked()
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
        with with_file_lock(self.events_path):
            return self._query_work_priority_hints_unlocked(items, project=project, session_id=session_id)

    def _query_work_priority_hints_unlocked(
        self,
        items: Iterable[Any],
        *,
        project: str = "",
        session_id: str = "",
    ) -> tuple[AffinityHint, ...]:
        nodes, edges = self._load_state_for_hint_unlocked()
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
        with with_file_lock(self.events_path):
            return self._query_research_priority_hints_unlocked(candidates, project=project, session_id=session_id)

    def _query_research_priority_hints_unlocked(
        self,
        candidates: Iterable[Any],
        *,
        project: str = "",
        session_id: str = "",
    ) -> tuple[AffinityHint, ...]:
        nodes, _edges = self._load_state_for_hint_unlocked()
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
        with with_file_lock(self.events_path):
            orphan_projection = not self.events_path.exists() and self.projection_path.exists()
            if orphan_projection:
                self._events_read_blocked = False
                self._events_blocked_reason = ""
                self.last_warnings = ()
                nodes, edges = self._load_projection_rows_unlocked()
                event_warnings = _bounded_warnings((*self.last_warnings, "affinity_events_missing"))
                projection = _projection_payload(nodes, edges, generated_at=_common.now_iso_z(), warnings=event_warnings)
                projection["diagnostic"] = {
                    "projection_only": True,
                    "source_events_missing": True,
                }
                return {
                    "schema_version": AFFINITY_SCHEMA_VERSION,
                    "affinity": projection,
                    "affinity_events": [],
                    "warnings": list(event_warnings),
                }
            events, nodes, edges = self._read_and_replay_unlocked()
            event_warnings = _bounded_warnings(self.last_warnings)
            if self._events_read_blocked:
                nodes, edges = self._load_projection_rows_unlocked()
            projection = _projection_payload(nodes, edges, generated_at=_common.now_iso_z(), warnings=event_warnings)
            return {
                "schema_version": AFFINITY_SCHEMA_VERSION,
                "affinity": projection,
                "affinity_events": events,
                "warnings": list(event_warnings),
            }

    def reset_all(self) -> bool:
        try:
            reset_event_backed_state(self.events_path, self.projection_path)
            self.last_warnings = ()
            self._events_read_blocked = False
            self._events_blocked_reason = ""
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
        try:

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
                        write_projection=False,
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

            result = self._mutate_event_log(decide)
            return result if isinstance(result, dict) else {"nodes": 0, "edges": 0, "warnings": ["affinity_error"]}
        except (OSError, TypeError, ValueError):
            if self._events_read_blocked:
                raise OSError("ghost affinity events are unreadable") from None
            raise

    def rebuild_from_events(self) -> bool:
        try:
            with with_file_lock(self.events_path):
                events = self._events_for_mutation_locked()
                nodes, edges = replay_affinity_events(events)
                self._write_projection(nodes, edges, warnings=self.last_warnings)
            return True
        except (OSError, TypeError, ValueError):
            return False

    def decay(self, *, min_interval_seconds: int = 0) -> dict[str, object]:
        interval = max(0, int(min_interval_seconds or 0))
        try:

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
                        write_projection=False,
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
                        write_projection=False,
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

            result = self._mutate_event_log(decide)
            return (
                result
                if isinstance(result, dict)
                else {
                    "removed_nodes": 0,
                    "removed_edges": 0,
                    "decayed_nodes": 0,
                    "decayed_edges": 0,
                    "skipped_reason": "affinity_error",
                    "warnings": list(self.last_warnings),
                }
            )
        except (OSError, TypeError, ValueError):
            if self._events_read_blocked:
                return {
                    "removed_nodes": 0,
                    "removed_edges": 0,
                    "decayed_nodes": 0,
                    "decayed_edges": 0,
                    "skipped_reason": self._events_blocked_reason or "events_read_blocked",
                    "warnings": list(self.last_warnings),
                }
            return {
                "removed_nodes": 0,
                "removed_edges": 0,
                "decayed_nodes": 0,
                "decayed_edges": 0,
                "skipped_reason": "affinity_error",
                "warnings": list(self.last_warnings),
            }

    def compact_if_needed(self) -> dict[str, object]:
        before = _event_file_stats(
            self.events_path,
            max_bytes=MAX_AFFINITY_EVENTS_BYTES,
            too_large_warning="affinity_events_too_large",
            unreadable_warning="affinity_events_unreadable",
        )
        try:
            with with_file_lock(self.events_path):
                before = _event_file_stats(
                    self.events_path,
                    max_bytes=MAX_AFFINITY_EVENTS_BYTES,
                    too_large_warning="affinity_events_too_large",
                    unreadable_warning="affinity_events_unreadable",
                )
                if not self.events_path.exists() and self.projection_path.exists():
                    warning = "affinity_events_missing"
                    self.last_warnings = (warning,)
                    return _compact_payload(False, False, before, before, (warning,), warning_cleaner=_bounded_warnings)
                if not before["readable"]:
                    warning = str(before["warning"] or "affinity_events_unreadable")
                    self.last_warnings = (warning,)
                    return _compact_payload(False, False, before, before, (warning,), warning_cleaner=_bounded_warnings)
                if not over_compact_budget(
                    before, max_events=MAX_AFFINITY_EVENTS, max_bytes=MAX_AFFINITY_EVENTS_BYTES
                ):
                    return _compact_payload(
                        True, False, before, before, self.last_warnings, warning_cleaner=_bounded_warnings
                    )
                events = self._events_for_mutation_locked()
                nodes, edges = replay_affinity_events(events)
                self._write_events_atomic([_snapshot_event(nodes, edges, ts=_common.now_iso_z(), reason="events_compacted")])
                self._write_projection(nodes, edges, warnings=self.last_warnings)
                after = _event_file_stats(
                    self.events_path,
                    max_bytes=MAX_AFFINITY_EVENTS_BYTES,
                    too_large_warning="affinity_events_too_large",
                    unreadable_warning="affinity_events_unreadable",
                )
                return _compact_payload(
                    True, after != before, before, after, self.last_warnings, warning_cleaner=_bounded_warnings
                )
        except (OSError, TypeError, ValueError):
            warning = self._events_blocked_reason or "affinity_compaction_failed"
            self.last_warnings = _bounded_warnings((*self.last_warnings, warning))
            return _compact_payload(
                False, False, before, before, self.last_warnings, warning_cleaner=_bounded_warnings
            )

    def _source_specs(
        self,
        *,
        hebbian_store: Any,
        work_queue_store: Any,
        research_interest_candidates: Iterable[Any],
        run_projection: Any,
        terminal_event: Mapping[str, object] | None,
        session_id: str,
        project: str,
    ) -> tuple[list[Any], list[Any]]:
        return collect_source_specs(
            hebbian_store=hebbian_store,
            work_queue_store=work_queue_store,
            research_interest_candidates=research_interest_candidates,
            run_projection=run_projection,
            terminal_event=terminal_event,
            session_id=session_id,
            project=project,
        )

    def _load_state_for_read_unlocked(self) -> tuple[list[AffinityNode], list[AffinityEdge]]:
        # One read performs one full replay: _read_and_replay_unlocked both
        # validates and projects in a single replay_affinity_events call.
        # No cross-round cache; the replayed rows are reused within this lock
        # only via the returned values.
        if self.events_path.exists():
            _events, nodes, edges = self._read_and_replay_unlocked()
            if not self._events_read_blocked:
                return nodes, edges
            return self._load_projection_rows_unlocked()
        return self._load_projection_rows_unlocked()

    def _load_state_for_hint_unlocked(self) -> tuple[list[AffinityNode], list[AffinityEdge]]:
        # Same single-replay contract as _load_state_for_read_unlocked, but
        # hints fail closed (empty) when the log is corrupt, while diagnostic
        # reads fall back to the projection.
        if self.events_path.exists():
            _events, nodes, edges = self._read_and_replay_unlocked()
            if self._events_read_blocked:
                return [], []
            return nodes, edges
        if self.projection_path.exists():
            self.last_warnings = ("affinity_events_missing",)
        return [], []

    def _load_projection_rows_unlocked(self) -> tuple[list[AffinityNode], list[AffinityEdge]]:
        payload, reason = read_projection_payload(
            self.projection_path,
            schema_version=AFFINITY_SCHEMA_VERSION,
            kind=_STATE_KIND,
            max_bytes=MAX_AFFINITY_STATE_BYTES,
        )
        if reason == "corrupt":
            backup_corrupt_file(self.projection_path)
            return [], []
        if payload is None:
            return [], []
        nodes = [
            node for node in (AffinityNode.from_payload(row) for row in _list(payload.get("nodes"))) if node is not None
        ]
        node_ids = {node.id for node in nodes}
        edges = [
            edge
            for edge in (AffinityEdge.from_payload(row) for row in _list(payload.get("edges")))
            if edge is not None and edge.source in node_ids and edge.target in node_ids
        ]
        return _bounded_nodes(nodes), _bounded_edges(edges, node_ids=node_ids)

    def _read_and_replay_unlocked(
        self,
    ) -> tuple[list[dict[str, object]], list[AffinityNode], list[AffinityEdge]]:
        """Read the event file and replay once.

        This is the single-replay path for reads: one
        ``replay_affinity_events`` call both validates (raising on illegal
        rows, including orphan edges) and produces the projected rows. No
        cross-round cache is kept; callers reuse the returned rows within the
        same file lock only.
        """
        self._events_read_blocked = False
        self._events_blocked_reason = ""
        read = self._event_log().read()
        if read.blocked:
            self._events_read_blocked = True
            self.last_warnings = _event_read_warnings(read.warnings)
            self._events_blocked_reason = "events_read_blocked"
            return [], [], []
        rows = list(read.rows)
        self.last_warnings = _event_read_warnings(read.warnings)
        try:
            nodes, edges = replay_affinity_events(rows)
        except (ValueError, TypeError, AttributeError):
            self.last_warnings = _bounded_warnings(("affinity_events.jsonl:semantic_invalid_event",))
            self._events_read_blocked = True
            self._events_blocked_reason = "events_read_blocked"
            return [], [], []
        return rows, nodes, edges

    def _read_events_unlocked(self) -> list[dict[str, object]]:
        """Return validated event rows for mutation paths.

        Uses the single events-owned implementation
        (:func:`affinity_events._affinity_events_replay_cleanly`) for semantic
        validation, so validation logic cannot drift from replay. This performs
        one full replay for validation; read paths avoid calling this and use
        :meth:`_read_and_replay_unlocked` instead so one read does one full
        replay total.
        """
        self._events_read_blocked = False
        self._events_blocked_reason = ""
        read = self._event_log().read()
        if read.blocked:
            self._events_read_blocked = True
            self.last_warnings = _event_read_warnings(read.warnings)
            self._events_blocked_reason = "events_read_blocked"
            return []
        rows = list(read.rows)
        self.last_warnings = _event_read_warnings(read.warnings)
        if not _affinity_events_replay_cleanly(rows):
            self.last_warnings = _bounded_warnings(("affinity_events.jsonl:semantic_invalid_event",))
            self._events_read_blocked = True
            self._events_blocked_reason = "events_read_blocked"
            return []
        return rows

    def _events_for_mutation_locked(self) -> list[dict[str, object]]:
        if not self.events_path.exists():
            if self.projection_path.exists():
                self._events_read_blocked = True
                self._events_blocked_reason = "affinity_events_missing"
                self.last_warnings = ("affinity_events_missing",)
                raise OSError("ghost affinity events are missing")
            self._events_read_blocked = False
            self._events_blocked_reason = ""
            self.last_warnings = ()
            return []
        events = self._read_events_unlocked()
        if self._events_read_blocked:
            raise OSError("ghost affinity events are unreadable")
        return events

    def _mutate_event_log(self, decide: Any) -> object:
        with with_file_lock(self.events_path):
            events = self._events_for_mutation_locked()
            mutation = decide(events)
            if not isinstance(mutation, _AffinityMutation):
                raise TypeError("invalid affinity mutation result")
            try:
                if mutation.replace_events is not None:
                    self._write_events_atomic(mutation.replace_events)
                elif mutation.append_events:
                    self._write_events_atomic((*events, *mutation.append_events))
            except (OSError, TypeError, ValueError):
                self.last_warnings = _bounded_warnings((*self.last_warnings, "affinity_event_write_failed"))
                raise
            if mutation.write_projection:
                self._write_projection_best_effort(mutation.nodes, mutation.edges)
            if mutation.compact:
                self._compact_if_needed_locked(mutation.nodes, mutation.edges)
            result = mutation.result
            if is_dataclass(result) and not isinstance(result, type) and hasattr(result, "warnings"):
                return replace(result, warnings=self.last_warnings)
            if isinstance(result, dict):
                return dict(result, warnings=list(self.last_warnings))
            return result

    def _write_events_atomic(self, events: Iterable[dict[str, object]]) -> None:
        self._event_log().write_atomic(events)

    def _write_projection(
        self,
        nodes: Iterable[AffinityNode],
        edges: Iterable[AffinityEdge],
        *,
        warnings: Iterable[str],
    ) -> None:
        write_json_atomic(
            self.projection_path,
            _projection_payload(nodes, edges, generated_at=_common.now_iso_z(), warnings=warnings),
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
            with contextlib.suppress(OSError):
                delete_file(self.projection_path)
            self.last_warnings = _bounded_warnings((*self.last_warnings, "affinity_projection_write_failed"))

    def _compact_if_needed_locked(self, nodes: Iterable[AffinityNode], edges: Iterable[AffinityEdge]) -> None:
        stats = _event_file_stats(
            self.events_path,
            max_bytes=MAX_AFFINITY_EVENTS_BYTES,
            too_large_warning="affinity_events_too_large",
            unreadable_warning="affinity_events_unreadable",
        )
        if not stats["readable"]:
            self.last_warnings = (str(stats["warning"] or "affinity_events_unreadable"),)
            return
        if not over_compact_budget(
            stats, max_events=MAX_AFFINITY_EVENTS, max_bytes=MAX_AFFINITY_EVENTS_BYTES
        ):
            return
        try:
            self._write_events_atomic([_snapshot_event(nodes, edges, ts=_common.now_iso_z(), reason="events_compacted")])
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


def _event_read_warnings(warnings: Iterable[str]) -> tuple[str, ...]:
    return event_read_warnings(warnings, stream="affinity_events", limit=MAX_AFFINITY_WARNINGS)


__all__ = [
    "AffinityEdge",
    "AffinityHint",
    "AffinityNode",
    "GhostAffinityStore",
    "GhostAffinitySyncResult",
    "apply_affinity_work_boost",
]
