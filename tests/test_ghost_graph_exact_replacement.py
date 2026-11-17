"""Exact graph replacement preserves unrelated deletes without bypassing fences."""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Literal

import pytest

from codey.ghost.control_surface import GhostControlSurface
from codey.ghost.graph_store import GhostGraph
from codey.ghost.sqlite import GhostSQLite
from tests.manual.ghost_memory_routing.deletion_runtime_probe import _seed

GraphName = Literal["hebbian", "affinity"]


def _node(identity: str) -> dict[str, object]:
    return {
        "id": identity, "kind": "style_preference", "scope": "project", "scope_ref": identity,
        "status": "active", "weight": 0.5, "candidate_ids": [f"candidate-{identity}"],
    }


def _edge(source: str, target: str) -> dict[str, object]:
    return {"source": source, "target": target, "relation": "related", "weight": 0.5}


def _graph(home: Path, name: GraphName) -> GhostGraph:
    graph = GhostGraph(GhostSQLite(home), name)
    with graph.transaction():
        graph.append([{"event_id": "seed", "kind": "fixture"}])
        graph.replace_rows([_node("a"), _node("b"), _node("c")], [_edge("a", "c"), _edge("b", "c")])
    return graph


def _retire(graph: GhostGraph) -> None:
    with graph.database.connection() as connection:
        connection.execute("INSERT INTO ghost_retired_inputs(kind,identity) VALUES('candidate','candidate-a')")


@pytest.mark.parametrize("name", ["hebbian", "affinity"])
def test_exact_retained_rows_survive_other_scope_removal_and_reopen(tmp_path: Path, name: GraphName) -> None:
    graph = _graph(tmp_path, name)
    _retire(graph)
    with graph.transaction():
        graph.append([{"event_id": "remove-b", "kind": "fixture"}])
        graph.replace_rows([_node("a"), _node("c")], [_edge("a", "c")])
    reopened = GhostGraph(GhostSQLite(tmp_path), name)
    assert reopened.rows() == ([_node("a"), _node("c")], [_edge("a", "c")])
    assert len(reopened.events()) == 2
    with reopened.transaction():
        reopened.append([{"event_id": "remove-a", "kind": "fixture"}])
        reopened.replace_rows([_node("c")], [])
    assert reopened.rows() == ([_node("c")], [])


@pytest.mark.parametrize("name", ["hebbian", "affinity"])
@pytest.mark.parametrize("change", ["payload", "alias", "edge_weight", "edge_relation"])
def test_retained_retired_node_does_not_allow_new_learning_and_rolls_back_event(
    tmp_path: Path, name: GraphName, change: str,
) -> None:
    graph = _graph(tmp_path, name)
    _retire(graph)
    nodes, edges = graph.rows()
    before = graph.rows(), graph.events()
    if change == "payload":
        nodes[0]["weight"] = 0.7
    elif change == "alias":
        nodes.append({**nodes[0], "id": "alias"})
    elif change == "edge_weight":
        edges[0]["weight"] = 0.7
    else:
        edges.append({**edges[0], "relation": "new-relation"})
    with pytest.raises(sqlite3.IntegrityError, match="deleted Ghost graph input"), graph.transaction():
        graph.append([{"event_id": "rejected", "kind": "fixture"}])
        graph.replace_rows(nodes, edges)
    reopened = GhostGraph(GhostSQLite(tmp_path), name)
    assert (reopened.rows(), reopened.events()) == before


@pytest.mark.parametrize("name", ["hebbian", "affinity"])
@pytest.mark.parametrize("column,value", [("scope_ref", "corrupt"), ("status", "inactive"), ("weight", 0.1)])
def test_replacement_repairs_indexed_columns_even_when_payload_is_unchanged(
    tmp_path: Path, name: GraphName, column: str, value: str | float,
) -> None:
    graph = _graph(tmp_path, name)
    desired = graph.rows()
    with graph.database.connection() as connection:
        connection.execute(f"UPDATE graph_nodes SET {column}=? WHERE graph=? AND id='a'", (value, name))
    graph.replace_rows(*desired)
    with graph.database.connection() as connection:
        row: sqlite3.Row | None = connection.execute(
            "SELECT scope_ref,status,weight FROM graph_nodes WHERE graph=? AND id='a'", (name,),
        ).fetchone()
        assert row is not None
        scope_ref: object = row[0]
        status: object = row[1]
        weight: object = row[2]
    assert (scope_ref, status, weight) == ("a", "active", 0.5)
    assert graph.rows() == desired


@pytest.mark.parametrize("name", ["hebbian", "affinity"])
@pytest.mark.parametrize("invalid", ["duplicate", "dangling_edge", "retired_column_repair"])
def test_failed_exact_replacement_restores_entire_graph_and_cursor(
    tmp_path: Path, name: GraphName, invalid: str,
) -> None:
    graph = _graph(tmp_path, name)
    if invalid == "retired_column_repair":
        _retire(graph)
        with graph.database.connection() as connection:
            connection.execute("UPDATE graph_nodes SET status='inactive' WHERE graph=? AND id='a'", (name,))
    nodes, edges = graph.rows()
    before = graph.rows(), graph.events()
    if invalid == "duplicate":
        nodes.append(dict(nodes[0]))
    elif invalid == "dangling_edge":
        edges.append(_edge("c", "missing"))
    with pytest.raises(sqlite3.IntegrityError), graph.transaction():
        graph.append([{"event_id": "rejected", "kind": "fixture"}])
        graph.replace_rows(nodes, edges)
    assert (GhostGraph(GhostSQLite(tmp_path), name).rows(), graph.events()) == before


def test_real_disjoint_project_deletes_preserve_unrelated_scope_and_can_rebuild(tmp_path: Path) -> None:
    surface = GhostControlSurface.from_state_home(tmp_path)
    a, b, c = (str(tmp_path / name) for name in ("a", "b", "c"))
    for project, run_id in ((a, "a-run"), (b, "b-run"), (c, "c-run")):
        _seed(surface, project, run_id=run_id)
    assert surface.inbox is not None and surface.hebbian is not None
    # The two coordinator transactions may commit inbox retirement before
    # either reaches the graph. No graph write should reinsert unrelated A/B.
    assert surface.inbox.delete_scope("project", project=a) == 1
    assert surface.inbox.delete_scope("project", project=b) == 1
    assert surface.hebbian.delete_scope("project", project=b)["nodes"] == 1
    assert {node.scope_ref for node in surface.hebbian.list_nodes()} == {a, c}
    assert surface.hebbian.delete_scope("project", project=a)["nodes"] == 1
    assert surface.hebbian.rebuild_from_events()
    assert {node.scope_ref for node in surface.hebbian.list_nodes()} == {c}
