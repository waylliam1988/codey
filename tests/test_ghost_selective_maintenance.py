"""Record control, replay, capacity and transaction boundaries of real owners."""
from __future__ import annotations

import sqlite3
import threading
from collections.abc import Iterable, Mapping
from dataclasses import replace
from pathlib import Path
from typing import Literal
from unittest.mock import patch

import pytest

from codey.ghost.affinity import GhostAffinityStore
from codey.ghost.affinity_events import _reinforce_edge, _reinforce_node, _snapshot_event
from codey.ghost.affinity_model import (
    MAX_AFFINITY_NODES,
    MAX_EDGE_OUT_DEGREE,
    AffinityEdgeSpec,
    AffinityNode,
    AffinityNodeSpec,
)
from codey.ghost.affinity_sources import collect_source_specs
from codey.ghost.continuity import MAX_CONTINUITY_ITEMS, GhostContinuityItem, GhostContinuityStore, _item, _item_event
from codey.ghost.graph_store import GhostGraph
from codey.ghost.hebbian import GhostEdge, GhostHebbianStore, _edge_event, _node_event
from codey.ghost.inbox import GhostInboxStore, GhostMaintenancePolicy
from codey.ghost.record_store import GhostRecords
from codey.ghost.sleep import GhostSleepStore
from codey.ghost.sqlite import GhostSQLite
from codey.knowledge.research_interest import ResearchInterestCandidate
from tests.test_ghost_maintenance_record_scope import BASE, Scope, Stores, _seed
from tests.test_ghost_maintenance_revocation import Barrier
from tests.test_ghost_queue_record_maintenance import _scope, _switch

HORIZON = "2026-12-02T00:00:00Z"
LATER = "2026-12-03T00:00:00Z"
OPERATIONS = ("hebbian", "affinity", "affinity_sync", "continuity")


def _fixture(home: Path, scope_name: str = "project") -> tuple[Stores, Scope, Scope]:
    target = _scope(home, scope_name, "target")
    peer = _scope(home, "project" if scope_name == "user" else scope_name, "peer")
    stores = _seed(home, target, peer)
    result = stores.continuity.sync_from_sources(hebbian_store=stores.hebbian, maintenance_as_of=BASE)
    assert result.ok and result.items_changed == 2
    return stores, target, peer


def _advance(stores: Stores, operation: str, *, as_of: str = HORIZON) -> bool:
    if operation == "hebbian":
        stores.hebbian.decay(maintenance_as_of=as_of)
        return True
    if operation == "affinity":
        stores.affinity.decay(maintenance_as_of=as_of)
        return True
    if operation == "affinity_sync":
        return stores.affinity.sync_from_sources(
            hebbian_store=stores.hebbian, maintenance=True, maintenance_as_of=as_of,
        ).ok
    if operation == "continuity":
        return stores.continuity.sync_from_sources(
            hebbian_store=stores.hebbian, maintenance=True, maintenance_as_of=as_of,
        ).ok
    raise ValueError(operation)


def _state(stores: Stores, operation: str) -> tuple[object, list[dict[str, object]]]:
    if operation == "hebbian":
        return stores.hebbian.graph.rows(), stores.hebbian.graph.events()
    if operation in {"affinity", "affinity_sync"}:
        return stores.affinity.graph.rows(), stores.affinity.graph.events()
    if operation == "continuity":
        return stores.continuity.records.rows(), stores.continuity.records.events()
    raise ValueError(operation)


def _scoped(stores: Stores, scope: Scope, operation: str) -> object:
    return stores.snapshot(scope)["affinity" if operation == "affinity_sync" else operation]


def _rebuild(stores: Stores) -> None:
    assert stores.hebbian.rebuild_from_events()
    assert stores.affinity.rebuild_from_events()
    assert stores.continuity.rebuild_from_events()
    assert stores.queue.rebuild_from_events()


@pytest.mark.parametrize("scope_name", ("project", "session"))
def test_sleep_replay_control_restore_and_delete_preserve_actual_membership(tmp_path: Path, scope_name: str) -> None:
    stores, target, peer = _fixture(tmp_path, scope_name)
    _switch(stores.inbox, target, False)
    before = stores.snapshot(target)
    peer_before = stores.snapshot(peer)
    report = GhostSleepStore(tmp_path).run_once(
        inbox_store=stores.inbox, hebbian_store=stores.hebbian, affinity_store=stores.affinity,
        continuity_store=stores.continuity, work_queue_store=stores.queue, maintenance_as_of=HORIZON,
    )
    assert not report.cancelled and not report.warnings and all(step.ok for step in report.steps)
    assert stores.snapshot(target) == before
    assert all(stores.snapshot(peer)[domain] != peer_before[domain] for domain in peer_before)
    assert not stores.continuity.list_items(scope=target.name, project=target.project, session_id=target.session,
                                            maintenance_as_of=HORIZON), "denied expired rows remain hidden to readers"
    actual = (stores.snapshot(target), stores.snapshot(peer))
    _switch(stores.inbox, target, True)
    cold = replace(stores, hebbian=GhostHebbianStore(tmp_path), affinity=GhostAffinityStore(tmp_path),
                   continuity=GhostContinuityStore(tmp_path))
    _rebuild(cold)
    assert (cold.snapshot(target), cold.snapshot(peer)) == actual, "current permission must not reinterpret history"
    for operation in OPERATIONS:
        assert _advance(cold, operation, as_of=LATER)
    assert all(cold.snapshot(target)[domain] != before[domain] for domain in ("hebbian", "affinity", "continuity"))
    _switch(stores.inbox, target, False)
    cold.hebbian.delete_scope(target.name, project=target.project, session_id=target.session)
    cold.affinity.delete_scope(target.name, project=target.project, session_id=target.session)
    cold.continuity.delete_scope(target.name, project=target.project, session_id=target.session)
    cold.queue.delete_scope(target.name, project=target.project, session_id=target.session)
    deleted = cold.snapshot(target)
    _rebuild(cold)
    assert cold.snapshot(target) == deleted
    for operation in OPERATIONS:
        assert _advance(cold, operation, as_of=LATER)
    assert cold.snapshot(target) == deleted


@pytest.mark.parametrize("operation", OPERATIONS)
@pytest.mark.parametrize("scope_name", ("user", "project", "session"))
@pytest.mark.parametrize("control", ("accept_events", "retrieve_projections", "admit_hints"))
def test_other_controls_do_not_substitute_for_maintenance(
    tmp_path: Path, operation: str, scope_name: str, control: str,
) -> None:
    stores, target, _peer = _fixture(tmp_path, scope_name)
    _switch(stores.inbox, target, False, control)
    before = _scoped(stores, target, operation)
    assert _advance(stores, operation)
    assert _scoped(stores, target, operation) != before


@pytest.mark.parametrize("operation", OPERATIONS)
@pytest.mark.parametrize("legacy", (False, True))
def test_global_and_legacy_deny_then_actual_reenable(tmp_path: Path, operation: str, legacy: bool) -> None:
    stores, _target, _peer = _fixture(tmp_path)
    if legacy:
        with stores.inbox.records.transaction():
            stores.inbox.records.set_setting("learning", {"schema_version": 1, "learning_enabled": False})
    else:
        _switch(stores.inbox, Scope("user", ""), False)
    before = _state(stores, operation)
    assert _advance(stores, operation)
    assert _state(stores, operation) == before
    _switch(stores.inbox, Scope("user", ""), True)
    assert _advance(stores, operation) and _state(stores, operation) != before


@pytest.mark.parametrize("operation", ("affinity_sync", "continuity"))
def test_normal_source_learning_remains_independent_of_maintenance(tmp_path: Path, operation: str) -> None:
    stores, _target, _peer = _fixture(tmp_path)
    _switch(stores.inbox, Scope("user", ""), False)
    before = _state(stores, operation)
    if operation == "affinity_sync":
        result = stores.affinity.sync_from_sources(hebbian_store=stores.hebbian, maintenance_as_of=HORIZON)
        assert result.ok and result.nodes_changed > 0
    else:
        refreshed = stores.continuity.sync_from_sources(hebbian_store=stores.hebbian, maintenance_as_of=HORIZON)
        assert refreshed.ok and refreshed.items_changed > 0
    assert _state(stores, operation) != before


@pytest.mark.parametrize("operation", OPERATIONS)
def test_unknown_parent_session_denied_while_allowed_user_advances(tmp_path: Path, operation: str) -> None:
    target, peer = Scope("session", "unknown-parent"), Scope("user", "")
    stores = _seed(tmp_path, target, peer)
    assert stores.continuity.sync_from_sources(hebbian_store=stores.hebbian, maintenance_as_of=BASE).ok
    _switch(stores.inbox, _scope(tmp_path, "project", "unknown"), False)
    before, peer_before = _scoped(stores, target, operation), _scoped(stores, peer, operation)
    assert _advance(stores, operation)
    assert _scoped(stores, target, operation) == before
    assert _scoped(stores, peer, operation) != peer_before


@pytest.mark.parametrize("operation", OPERATIONS)
@pytest.mark.parametrize("fault", ("entry", "value", "read"))
def test_policy_fault_is_closed_and_cold_retry_recovers(tmp_path: Path, operation: str, fault: str) -> None:
    stores, _target, _peer = _fixture(tmp_path)
    if fault != "read":
        entry: object = "bad-entry" if fault == "entry" else {"run_maintenance": "bad-value"}
        with stores.inbox.records.transaction():
            stores.inbox.records.set_setting("learning", {
                "schema_version": 1, "learning_enabled": True, "control_scopes": {"session": {"other": entry}},
            })
    before = _state(stores, operation)
    try:
        if fault == "read":
            with patch.object(GhostInboxStore, "maintenance_policy", side_effect=sqlite3.OperationalError("read failed")):
                ok = _advance(stores, operation)
        else:
            ok = _advance(stores, operation)
    except (OSError, ValueError, sqlite3.Error):
        ok = False
    assert not ok and _state(stores, operation) == before
    with stores.inbox.records.transaction():
        stores.inbox.records.set_setting("learning", {"schema_version": 1, "learning_enabled": True})
    assert _advance(stores, operation) and _state(stores, operation) != before


@pytest.mark.parametrize("domain", ("hebbian", "affinity"))
def test_denied_cross_edge_pins_low_allowed_endpoint_without_blocking_peer(tmp_path: Path, domain: str) -> None:
    stores, target, peer = _fixture(tmp_path)
    if domain == "hebbian":
        nodes = list(stores.hebbian.list_nodes())
        denied = next(node for node in nodes if node.scope_ref == target.raw_ref)
        allowed = next(node for node in nodes if node.scope_ref == peer.raw_ref)
        pinned = replace(allowed, id="pinned-endpoint", weight=0.0501)
        edge = GhostEdge(denied.id, pinned.id, "coactivated_with", 0.2, (), (), BASE, BASE, BASE)
        with stores.hebbian.graph.transaction():
            stores.hebbian.graph.append([_node_event(pinned, action="upsert", ts=BASE), _edge_event(edge, action="upsert", ts=BASE)])
            stores.hebbian.graph.replace_rows((node.to_payload() for node in (*nodes, pinned)), [edge.to_payload()])
        before = stores.hebbian.graph.rows()
        _switch(stores.inbox, target, False)
        assert _advance(stores, domain)
        after = stores.hebbian.graph.rows()
        assert pinned.to_payload() in after[0] and after[1] == before[1]
        assert allowed.to_payload() not in after[0]
    else:
        original_nodes, _original_edges = stores.affinity.graph.rows()
        decoded_nodes = [AffinityNode.from_payload(row) for row in original_nodes]
        assert all(node is not None for node in decoded_nodes)
        live = [node for node in decoded_nodes if node is not None]
        denied_node = next(node for node in live if node.scope_ref == target.stored_ref("affinity"))
        allowed_node = next(node for node in live if node.scope_ref == peer.stored_ref("affinity"))
        pinned_node = replace(allowed_node, weight=0.0401)
        updated = [pinned_node if node.id == pinned_node.id else node for node in live]
        cross_edge, _changed = _reinforce_edge(None, AffinityEdgeSpec(
            denied_node.id, pinned_node.id, "associated_with", peer.name, peer.stored_ref("affinity"),
            0.8, 1.0, ("edge-source",), (),
        ), now=BASE)
        with stores.affinity.graph.transaction():
            stores.affinity.graph.append([_snapshot_event(updated, [cross_edge], ts=BASE, reason="fixture")], replace=True)
            stores.affinity.graph.replace_rows((node.to_payload() for node in updated), [cross_edge.to_payload()])
        before = stores.affinity.graph.rows()
        _switch(stores.inbox, target, False)
        assert _advance(stores, domain)
        after = stores.affinity.graph.rows()
        assert pinned_node.to_payload() in after[0] and after[1] == before[1]
        assert after != before
    actual = _state(stores, domain)[0]
    _rebuild(stores)
    assert _state(stores, domain)[0] == actual


def _research(scope: Scope, concepts: tuple[str, ...]) -> ResearchInterestCandidate:
    return ResearchInterestCandidate("incoming", "Review concepts", concepts, (), ("incoming-source",),
                                     scope.name, scope.raw_ref, 0.9, 0.9, "Review saved concepts", "research", "incoming")


def _concept(scope: Scope, key: str, *, weight: float = 0.8) -> AffinityNode:
    node, _changed = _reinforce_node(None, AffinityNodeSpec(
        "research_concept", key, f"Concept {key}", scope.name, scope.stored_ref("affinity"),
        0.9, 1.0, (f"initial:{key}",), (),
    ), now=BASE)
    return replace(node, weight=weight)


def test_affinity_full_node_capacity_rejects_only_unsafe_addition(tmp_path: Path) -> None:
    stores, target, peer = _fixture(tmp_path)
    specs, _edge_specs = collect_source_specs(
        hebbian_store=stores.hebbian, work_queue_store=None, research_interest_candidates=(),
        run_projection=None, terminal_event=None, session_id="", project="", maintenance_as_of=HORIZON,
    )
    peer_spec = next(spec for spec in specs if spec.scope_ref == peer.stored_ref("affinity"))
    existing, _changed = _reinforce_node(None, replace(peer_spec, source_refs=("initial-peer",)), now=BASE)
    existing = replace(existing, weight=0.9)
    protected = [_concept(target, f"protected-{index}", weight=0.041) for index in range(MAX_AFFINITY_NODES - 1)]
    with stores.affinity.graph.transaction():
        stores.affinity.graph.append([_snapshot_event((*protected, existing), (), ts=BASE, reason="fixture")], replace=True)
        stores.affinity.graph.replace_rows((node.to_payload() for node in (*protected, existing)), ())
    _switch(stores.inbox, target, False)
    result = stores.affinity.sync_from_sources(
        hebbian_store=stores.hebbian, research_interest_candidates=(_research(peer, ("unsafe-new",)),),
        maintenance=True, maintenance_as_of=HORIZON,
    )
    assert result.ok and result.nodes_changed == 1
    nodes, _edges = stores.affinity.graph.rows()
    assert len(nodes) == MAX_AFFINITY_NODES and all(node.to_payload() in nodes for node in protected)
    assert existing.to_payload() not in nodes and not any(row.get("key") == "unsafe-new" for row in nodes)
    actual = stores.affinity.graph.rows()
    _switch(stores.inbox, target, True)
    assert stores.affinity.rebuild_from_events() and stores.affinity.graph.rows() == actual


def test_affinity_degree_capacity_preserves_edge_own_denied_scope(tmp_path: Path) -> None:
    stores, target, peer = _fixture(tmp_path)
    center, incoming = _concept(peer, "center"), _concept(peer, "incoming")
    neighbors = [_concept(peer, f"neighbor-{index}") for index in range(MAX_EDGE_OUT_DEGREE)]
    edges = [_reinforce_edge(None, AffinityEdgeSpec(
        center.id, node.id, "associated_with", target.name, target.stored_ref("affinity"),
        0.8, 1.0, (f"protected-edge-{index}",), (),
    ), now=BASE)[0] for index, node in enumerate(neighbors)]
    edges = [replace(edge, weight=0.011) for edge in edges]
    nodes = [center, incoming, *neighbors]
    with stores.affinity.graph.transaction():
        stores.affinity.graph.append([_snapshot_event(nodes, edges, ts=BASE, reason="fixture")], replace=True)
        stores.affinity.graph.replace_rows((node.to_payload() for node in nodes), (edge.to_payload() for edge in edges))
    _switch(stores.inbox, target, False)
    result = stores.affinity.sync_from_sources(research_interest_candidates=(_research(peer, ("center", "incoming")),),
                                              maintenance=True, maintenance_as_of=HORIZON)
    assert result.ok and result.nodes_changed == 1 and result.edges_changed == 0
    after_nodes, after_edges = stores.affinity.graph.rows()
    assert len(after_edges) == len(edges) and all(edge.to_payload() in after_edges for edge in edges)
    assert center.to_payload() in after_nodes and incoming.to_payload() not in after_nodes
    assert _advance(stores, "affinity")
    assert all(edge.to_payload() in stores.affinity.graph.rows()[1] for edge in edges)
    actual = stores.affinity.graph.rows()
    assert stores.affinity.rebuild_from_events() and stores.affinity.graph.rows() == actual


def test_continuity_reserves_protected_capacity_and_records_actual_eviction(tmp_path: Path) -> None:
    stores, target, peer = _fixture(tmp_path)
    protected = [_item(kind="recently_reinforced_preference", scope=target.name, scope_ref=target.raw_ref,
                       text="Prefer concise replies.", source="hebbian", source_ref=f"protected-{index}",
                       weight=0.01, confidence=0.9, now=HORIZON) for index in range(MAX_CONTINUITY_ITEMS - 1)]
    old = _item(kind="recently_reinforced_preference", scope=peer.name, scope_ref=peer.raw_ref,
                text="Prefer concise replies.", source="hebbian", source_ref="old-allowed", weight=0.02,
                confidence=0.9, now=HORIZON)
    with stores.continuity.records.transaction():
        stores.continuity.records.append((_item_event(item, action="upsert", ts=HORIZON)
                                         for item in (*protected, old)), replace=True)
        stores.continuity.records.replace_rows(item.to_payload() for item in (*protected, old))
    _switch(stores.inbox, target, False)
    assert _advance(stores, "continuity")
    rows = stores.continuity.records.rows()
    assert len(rows) == MAX_CONTINUITY_ITEMS and all(item.to_payload() in rows for item in protected)
    assert old.to_payload() not in rows
    assert stores.continuity.rebuild_from_events() and stores.continuity.records.rows() == rows
    # A later deletion must not allow the evicted old allowed row to reappear.
    stores.continuity.delete_scope(peer.name, project=peer.project)
    assert stores.continuity.rebuild_from_events()
    assert stores.continuity.records.rows() == [item.to_payload() for item in sorted(protected, key=_continuity_id)]


def _continuity_id(item: GhostContinuityItem) -> str:
    return item.id


@pytest.mark.parametrize("domain", ("hebbian", "affinity", "continuity"))
def test_real_over_budget_checkpoint_defers_then_restores(tmp_path: Path, domain: str) -> None:
    stores, target, _peer = _fixture(tmp_path)
    _switch(stores.inbox, target, False)
    original = _state(stores, domain)[1]
    budget = {"hebbian": "codey.ghost.hebbian.MAX_HEBBIAN_EVENTS", "affinity": "codey.ghost.affinity.MAX_AFFINITY_EVENTS",
              "continuity": "codey.ghost.continuity.MAX_CONTINUITY_EVENTS"}[domain]
    store = stores.hebbian if domain == "hebbian" else stores.affinity if domain == "affinity" else stores.continuity
    with patch(budget, 1):
        assert _advance(stores, domain)
        assert store.compact_if_needed()["compacted"] is False
        events = _state(stores, domain)[1]
        assert events[:len(original)] == original and len(events) > len(original)
        actual = _state(stores, domain)[0]
        _switch(stores.inbox, target, True)
        assert store.compact_if_needed()["compacted"] is True
        _rebuild(stores)
        assert _state(stores, domain)[0] == actual


class FailingGraph(GhostGraph):
    def __init__(self, database: GhostSQLite, name: Literal["hebbian", "affinity"], fault: str) -> None:
        super().__init__(database, name)
        self.fault = fault

    def append(self, events: Iterable[Mapping[str, object]], *, replace: bool = False) -> None:
        super().append(events, replace=replace)
        if self.fault == "events":
            raise sqlite3.OperationalError("failure after actual graph append")

    def replace_rows(self, nodes: Iterable[Mapping[str, object]], edges: Iterable[Mapping[str, object]]) -> None:
        super().replace_rows(nodes, edges)
        if self.fault == "projection":
            raise sqlite3.OperationalError("failure after actual graph projection")


class FailingRecords(GhostRecords):
    def __init__(self, database: GhostSQLite, fault: str) -> None:
        super().__init__(database, "continuity")
        self.fault = fault

    def append(self, events: Iterable[Mapping[str, object]], *, replace: bool = False) -> None:
        super().append(events, replace=replace)
        if self.fault == "events":
            raise sqlite3.OperationalError("failure after actual continuity append")

    def replace_rows(self, rows: Iterable[Mapping[str, object]]) -> None:
        super().replace_rows(rows)
        if self.fault == "projection":
            raise sqlite3.OperationalError("failure after actual continuity projection")


@pytest.mark.parametrize("operation", OPERATIONS)
@pytest.mark.parametrize("fault", ("events", "projection"))
def test_actual_write_failure_rolls_back_and_cold_retry_recovers(tmp_path: Path, operation: str, fault: str) -> None:
    stores, target, peer = _fixture(tmp_path)
    _switch(stores.inbox, target, False)
    before = _state(stores, operation)
    if operation == "hebbian":
        stores.hebbian.graph = FailingGraph(stores.hebbian.database, "hebbian", fault)
    elif operation in {"affinity", "affinity_sync"}:
        stores.affinity.graph = FailingGraph(stores.affinity.database, "affinity", fault)
    else:
        stores.continuity.records = FailingRecords(stores.continuity.database, fault)
    try:
        ok = _advance(stores, operation)
    except (OSError, ValueError, sqlite3.Error):
        ok = False
    assert not ok and _state(stores, operation) == before
    cold = replace(stores, hebbian=GhostHebbianStore(tmp_path), affinity=GhostAffinityStore(tmp_path),
                   continuity=GhostContinuityStore(tmp_path))
    peer_before = _scoped(cold, peer, operation)
    target_before = _scoped(cold, target, operation)
    assert _advance(cold, operation)
    assert _scoped(cold, peer, operation) != peer_before and _scoped(cold, target, operation) == target_before


@pytest.mark.parametrize("operation", OPERATIONS)
@pytest.mark.parametrize("scope_name", ("project", "session"))
@pytest.mark.parametrize("deny_first", (False, True))
def test_record_admission_serializes_both_concurrent_commit_orders(
    tmp_path: Path, operation: str, scope_name: str, deny_first: bool,
) -> None:
    stores, target, peer = _fixture(tmp_path, scope_name)
    barrier = Barrier()
    original_policy = GhostInboxStore.maintenance_policy
    attempted, finished, committed = threading.Event(), threading.Event(), threading.Event()
    errors: list[str] = []
    results: list[bool] = []
    committed_target: list[object] = []
    peer_before = _scoped(stores, peer, operation)

    def policy(owner: GhostInboxStore) -> GhostMaintenancePolicy:
        snapshot = original_policy(owner)
        if not barrier.reached.is_set():
            barrier.pause()
        return snapshot

    def work() -> None:
        attempted.set()
        try:
            results.append(_advance(stores, operation))
        except Exception as exc:
            errors.append(f"worker {type(exc).__name__}: {exc}")
        finally:
            finished.set()

    def revoke() -> None:
        attempted.set()
        try:
            with GhostSQLite(tmp_path).write_transaction():
                _switch(stores.inbox, target, False)
                committed_target.append(_scoped(stores, target, operation))
            committed.set()
        except Exception as exc:
            errors.append(f"writer {type(exc).__name__}: {exc}")

    worker, writer = threading.Thread(target=work, daemon=True), threading.Thread(target=revoke, daemon=True)
    if deny_first:
        before = _scoped(stores, target, operation)
        with GhostSQLite(tmp_path).write_transaction():
            _switch(stores.inbox, target, False)
            worker.start()
            assert attempted.wait(10) and not finished.wait(0.2)
        worker.join(10)
        assert not worker.is_alive() and not errors and results == [True], errors
        assert _scoped(stores, target, operation) == before
    else:
        with patch.object(GhostInboxStore, "maintenance_policy", policy):
            try:
                worker.start()
                assert barrier.reached.wait(10)
                attempted.clear()
                writer.start()
                assert attempted.wait(10) and not committed.wait(0.2)
            finally:
                barrier.release.set()
            worker.join(10)
            writer.join(10)
        assert not worker.is_alive() and not writer.is_alive() and not errors and results == [True], errors
        assert committed.is_set() and committed_target == [_scoped(stores, target, operation)]
    assert _scoped(stores, peer, operation) != peer_before
    protected = _scoped(stores, target, operation)
    assert _advance(stores, operation, as_of=LATER) and _scoped(stores, target, operation) == protected
