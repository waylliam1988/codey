"""Tie-bound graphs retain canonical state through ordinary lifecycle owners."""

from __future__ import annotations

import sqlite3
from collections.abc import Iterable, Mapping
from contextlib import closing
from dataclasses import replace
from itertools import permutations
from pathlib import Path
from unittest.mock import patch

import pytest

from codey.app.context import AppContext
from codey.ghost.affinity import GhostAffinityStore
from codey.ghost.affinity_events import _edge_reinforced_event, _node_reinforced_event, replay_affinity_events
from codey.ghost.affinity_model import AffinityEdgeSpec, AffinityNodeSpec
from codey.ghost.control_surface import GhostControlSurface
from codey.ghost.graph_primitives import bound_graph_edges, bound_graph_nodes
from codey.ghost.hebbian import MAX_HEBBIAN_EVENTS, GhostHebbianStore
from codey.ghost.inbox import GhostInboxStore
from codey.ghost.sleep import GhostSleepStore
from tests.test_ghost_graph_tie_replay import NOW, _seed

NEXT = "2026-11-19T00:00:00Z"


def _history(store: GhostHebbianStore) -> tuple[str, ...]:
    with store.database.connection() as connection:
        rows: Iterable[sqlite3.Row] = connection.execute(
            "SELECT payload FROM graph_events WHERE graph='hebbian' ORDER BY sequence",
        )
        payloads: list[str] = []
        for row in rows:
            payload: object = row[0]
            assert isinstance(payload, str)
            payloads.append(payload)
    return tuple(payloads)


def _reopen_rebuild(home: Path) -> GhostHebbianStore:
    store = GhostHebbianStore(home)
    before, events = store.graph.rows(), store.graph.events()
    assert store.rebuild_from_events()
    assert store.graph.rows() == before and store.graph.events() == events
    assert store.rebuild_from_events() and store.graph.rows() == before
    return store


@pytest.mark.parametrize("scope", ("user", "project", "session"))
@pytest.mark.parametrize("action", ("sleep", "compact", "revoke", "delete_scope", "reset_all", "expire", "disabled", "fault"))
def test_public_tied_graph_lifecycle_keeps_replay_and_original_history(
    tmp_path: Path, scope: str, action: str,
) -> None:
    with patch("codey.ghost._common.now_iso_z", return_value=NOW):
        store, _strong = _seed(tmp_path, scope, tied=True)
        history = _history(store)
        before = store.graph.rows()
        if action == "sleep":
            affinity = GhostAffinityStore(tmp_path)
            report = GhostSleepStore(tmp_path).run_once(
                inbox_store=GhostInboxStore(tmp_path), hebbian_store=store, affinity_store=affinity,
                session_id="s1", project=str(tmp_path / "project"), maintenance_as_of=NEXT,
            )
            assert all(step.ok for step in report.steps), report
            visible = affinity.list_nodes(project=str(tmp_path / "project"), session_id="s1")
            assert len(visible) == len(store.list_nodes())
            # This source maps accepted memory nodes, not Hebbian edges.
            assert not affinity.list_edges(project=str(tmp_path / "project"), session_id="s1")
            assert affinity.query_directive_order_hints(
                store.list_nodes(), project=str(tmp_path / "project"), session_id="s1",
            )
            affinity_before = affinity.graph.rows()
            assert GhostAffinityStore(tmp_path).rebuild_from_events()
            assert affinity.graph.rows() == affinity_before
        elif action == "compact":
            # Use the real event budget. Repeating the same valid node payload
            # with distinct audit actions changes neither weight nor sources.
            event = next(event for event in store.graph.events() if event["type"] == "ghost_hebbian_node_upsert")
            padding = ({**event, "action": f"tie-capacity-{index}"} for index in range(MAX_HEBBIAN_EVENTS + 1))
            with store.graph.transaction():
                store.graph.append(padding)
                store.graph.replace_rows(*before)
            result = store.compact_if_needed()
            assert result["compacted"] is True, result
            assert store.graph.rows() == before
        elif action == "revoke":
            inbox = GhostInboxStore(tmp_path)
            candidate_id = store.list_nodes()[0].candidate_ids[0]
            reviewed = inbox.review_candidate(candidate_id, "reject", reviewed_by="test")
            assert reviewed is not None and reviewed.status == "rejected"
            store.sync_from_inbox(inbox)
            assert all(candidate_id not in node.candidate_ids for node in store.list_nodes())
        elif action in {"delete_scope", "reset_all"}:
            surface = GhostControlSurface.from_state_home(tmp_path)
            assert surface.inbox is not None
            for control in ("accept_events", "retrieve_projections", "run_maintenance", "admit_hints"):
                assert surface.inbox.set_control(control, False, scope="user")
            status, payload = surface.dispatch_action({
                "action": action, "scope": scope, "project": str(tmp_path / "project"),
                "session_id": "s1", "confirm": True,
            })
            assert status == 200 and payload["ok"] is True, payload
            assert not store.list_nodes() and not store.list_edges()
        elif action == "expire":
            result = store.decay(maintenance_as_of="2036-11-18T00:00:00Z")
            assert result["removed_edges"] == 8
            assert not store.list_nodes(status="active") and not store.list_edges()
        elif action == "disabled":
            inbox = GhostInboxStore(tmp_path)
            for control in ("accept_events", "retrieve_projections", "run_maintenance", "admit_hints"):
                assert inbox.set_control(control, False, scope="user")
            # The application owns the automatic scheduling control. Direct
            # explicit store maintenance remains available for recovery.
            with closing(AppContext(tmp_path)) as state:
                assert not state.kick_ghost_sleep(session_id="s1", project=str(tmp_path / "project"))
                assert state.wait_for_ghost_sleep(timeout=5)
            assert store.graph.rows() == before
        else:
            assert action == "fault"

            def fail_write(nodes: Iterable[Mapping[str, object]], edges: Iterable[Mapping[str, object]]) -> None:
                raise sqlite3.OperationalError("injected graph projection failure")

            events = store.graph.events()
            with (
                patch.object(store.graph, "replace_rows", fail_write),
                pytest.raises(sqlite3.OperationalError, match="injected graph projection failure"),
            ):
                store.decay(maintenance_as_of=NEXT)
            assert store.graph.rows() == before and store.graph.events() == events
            assert _history(store) == history
            assert store.decay(maintenance_as_of=NEXT)["skipped_reason"] == ""
        # A change to ranking must not physically rewrite history or make
        # current rows differ after a cold explicit recovery.
        assert _history(store)[:len(history)] == history
        reopened = _reopen_rebuild(tmp_path)
        if action in {"delete_scope", "reset_all"}:
            assert not reopened.list_nodes() and not reopened.list_edges()


@pytest.mark.parametrize("order", range(6))
def test_affinity_event_rows_use_semantic_ties_and_keep_active_filter(order: int) -> None:
    events: list[dict[str, object]] = []
    for key in ("a", "b", "c"):
        spec = AffinityNodeSpec(
            kind="task_type", key=key, label=key, scope="user", scope_ref="", confidence=1.0,
            reward=1.0, source_refs=("work_item:" + key,),
        )
        events.append(_node_reinforced_event(spec, ts=NOW))
    nodes, _ = replay_affinity_events(events)
    assert len(nodes) == 3
    for relation in ("associated_with", "prefers_for", "works_well_for"):
        edge_spec = AffinityEdgeSpec(
            source=nodes[0].id, target=nodes[1].id, relation=relation, scope="user", scope_ref="",
            confidence=1.0, reward=1.0, source_refs=("work_item:" + relation,),
        )
        events.append(_edge_reinforced_event(edge_spec, ts=NOW))
    nodes, edges = replay_affinity_events(events)
    assert len(edges) == 3
    bounded_nodes = bound_graph_nodes(list(permutations(nodes))[order], min_active_weight=0.04, limit=2)
    assert [node.id for node in bounded_nodes] == sorted(node.id for node in nodes)[:2]
    shuffled = list(permutations(edges))[order]
    # A stronger expired edge must remain filtered. Edge audit IDs are not
    # ranking keys; relations distinguish the remaining identical endpoints.
    expired = replace(edges[0], status="expired", weight=1.0)
    bounded_edges = bound_graph_edges(
        (*shuffled, expired), node_ids={node.id for node in nodes}, min_weight=0.01,
        limit=10, max_out_degree=1, require_active=True,
    )
    assert len(bounded_edges) == 1 and bounded_edges[0].relation == "associated_with"
