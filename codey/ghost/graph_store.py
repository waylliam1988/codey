"""Atomic event, graph and consumer-position ownership inside ghost.sqlite3."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from collections.abc import Generator, Iterable, Mapping
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Literal

from codey.ghost.payload_types import decoded_object
from codey.ghost.sqlite import GhostSQLite


def _encode(value: Mapping[str, object]) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _decoded_row(row: sqlite3.Row) -> dict[str, object]:
    encoded: object = row[0]
    return decoded_object(encoded)


def _stored_values(row: sqlite3.Row) -> tuple[object, ...]:
    values: list[object] = []
    for index in range(len(row)):
        value: object = row[index]
        values.append(value)
    return tuple(values)


class GhostGraph:
    """One of the two learning graphs; no file projection or fallback owner.

    A bound connection is local to the calling context, never shared between
    threads. Nested graph methods reuse the caller's explicit transaction so
    events, graph rows and the consumer cursor either all commit or all roll back.
    """

    def __init__(self, database: GhostSQLite, name: Literal["hebbian", "affinity"]) -> None:
        self.database = database
        self.name = name
        self._connection: ContextVar[sqlite3.Connection | None] = ContextVar("ghost_graph_transaction", default=None)

    @contextmanager
    def transaction(self, *, write: bool = True, deleting: bool = False) -> Generator[None]:
        if deleting and not write:
            raise ValueError("deletion requires a write transaction")
        if self._connection.get() is not None:
            if write:
                self.database.check_projection_inputs(self._db())
            if deleting:
                self.database.advance_deletion(self._db())
            yield
            return
        with self.database.connection() as connection:
            if not connection.in_transaction:
                connection.execute("BEGIN IMMEDIATE" if write else "BEGIN")
            if write:
                self.database.check_projection_inputs(connection)
            if deleting:
                self.database.advance_deletion(connection)
            token = self._connection.set(connection)
            try:
                yield
                if write:
                    # append (including replace=True) must be paired with replace_rows
                    # in this transaction. Reject and roll back an unpaired event write.
                    self._check_cursor()
            finally:
                self._connection.reset(token)

    def _db(self) -> sqlite3.Connection:
        connection = self._connection.get()
        if connection is None:
            raise RuntimeError("graph access requires its transaction")
        return connection

    def events(self) -> list[dict[str, object]]:
        with self.transaction(write=False):
            rows: Iterable[sqlite3.Row] = self._db().execute(
                "SELECT payload FROM graph_events WHERE graph=? ORDER BY sequence", (self.name,),
            )
            return [
                _decoded_row(row)
                for row in rows
            ]

    def rows(self) -> tuple[list[dict[str, object]], list[dict[str, object]]]:
        with self.transaction(write=False):
            database = self._db()
            self._check_cursor()
            node_rows: Iterable[sqlite3.Row] = database.execute(
                "SELECT payload FROM graph_nodes WHERE graph=? ORDER BY id", (self.name,),
            )
            nodes = [_decoded_row(row) for row in node_rows]
            edge_rows: Iterable[sqlite3.Row] = database.execute(
                "SELECT payload FROM graph_edges WHERE graph=? ORDER BY source,target,relation", (self.name,),
            )
            edges = [_decoded_row(row) for row in edge_rows]
            return nodes, edges

    def _check_cursor(self) -> None:
        database = self._db()
        tail_row: sqlite3.Row | None = database.execute(
            "SELECT coalesce(max(sequence),0) FROM graph_events WHERE graph=?", (self.name,)
        ).fetchone()
        tail: object = None
        if tail_row is not None:
            tail = tail_row[0]
        cursor: sqlite3.Row | None = database.execute(
            "SELECT applied_sequence FROM graph_consumers WHERE graph=?", (self.name,),
        ).fetchone()
        position: object = 0
        if cursor is not None:
            position = cursor[0]
        if type(tail) is not int or type(position) is not int or tail != position:
            raise OSError("Ghost graph consumer position disagrees with committed events")

    def append(self, events: Iterable[Mapping[str, object]], *, replace: bool = False) -> None:
        with self.transaction():
            database = self._db()
            if replace:
                database.execute("DELETE FROM graph_events WHERE graph=?", (self.name,))
            for event in events:
                payload = _encode(event)
                identity = str(event.get("event_id") or hashlib.sha256(payload.encode()).hexdigest())
                existing: sqlite3.Row | None = database.execute(
                    "SELECT payload FROM graph_events WHERE graph=? AND event_id=?", (self.name, identity)
                ).fetchone()
                if existing is not None:
                    previous: object = existing[0]
                    if previous != payload:
                        raise ValueError("Ghost graph event identity conflict")
                    continue
                database.execute(
                    "INSERT INTO graph_events(graph,event_id,payload) VALUES(?,?,?)", (self.name, identity, payload)
                )

    def replace_rows(self, nodes: Iterable[Mapping[str, object]], edges: Iterable[Mapping[str, object]]) -> None:
        with self.transaction():
            database = self._db()
            stored_nodes: Iterable[sqlite3.Row] = database.execute(
                "SELECT id,kind,scope,scope_ref,status,weight,payload FROM graph_nodes WHERE graph=?", (self.name,),
            )
            previous_nodes: dict[str, tuple[object, ...]] = {}
            for row in stored_nodes:
                values = _stored_values(row)
                stored_identity = values[0]
                if not isinstance(stored_identity, str):
                    raise OSError("invalid stored Ghost graph identity")
                previous_nodes[stored_identity] = values
            stored_edges: Iterable[sqlite3.Row] = database.execute(
                "SELECT source,target,relation,weight,payload FROM graph_edges WHERE graph=?", (self.name,),
            )
            previous_edges: set[tuple[object, ...]] = {_stored_values(row) for row in stored_edges}
            database.execute("DELETE FROM graph_edges WHERE graph=?", (self.name,))
            seen: set[str] = set()
            for node in nodes:
                identity = node["id"]
                if not isinstance(identity, str):
                    raise ValueError("Ghost graph node identity must be a string")
                if identity in seen:
                    raise sqlite3.IntegrityError("duplicate Ghost graph node identity")
                seen.add(identity)
                desired = (identity, node["kind"], node["scope"], node["scope_ref"], node["status"], node["weight"], _encode(node))
                # A different scope may already have retired this unchanged
                # row's inputs while its own graph deletion is still pending.
                # Preserve it exactly; changed/new rows still use INSERT and
                # its retired-input guard, including indexed-column repairs.
                if previous_nodes.pop(identity, None) == desired:
                    continue
                database.execute("DELETE FROM graph_nodes WHERE graph=? AND id=?", (self.name, identity))
                database.execute(
                    "INSERT INTO graph_nodes(graph,id,kind,scope,scope_ref,status,weight,payload) VALUES(?,?,?,?,?,?,?,?)",
                    (self.name, *desired),
                )
            for identity in previous_nodes:
                database.execute("DELETE FROM graph_nodes WHERE graph=? AND id=?", (self.name, identity))
            for edge in edges:
                desired_edge = (edge["source"], edge["target"], edge["relation"], edge["weight"], _encode(edge))
                if desired_edge not in previous_edges:
                    # Retaining a node must not permit new learning through a
                    # new/changed edge attached to its retired candidate.
                    retired: sqlite3.Row | None = database.execute(
                        "SELECT 1 FROM graph_nodes AS node, json_each(node.payload,'$.candidate_ids') AS candidate "
                        "JOIN ghost_retired_inputs AS retired ON retired.identity=candidate.value "
                        "WHERE node.graph=? AND node.id IN (?,?) AND retired.kind='candidate' LIMIT 1",
                        (self.name, edge["source"], edge["target"]),
                    ).fetchone()
                    if retired is not None:
                        raise sqlite3.IntegrityError("deleted Ghost graph input")
                database.execute(
                    "INSERT INTO graph_edges(graph,source,target,relation,weight,payload) VALUES(?,?,?,?,?,?)",
                    (self.name, *desired_edge),
                )
            database.execute(
                "INSERT INTO graph_consumers(graph,applied_sequence) SELECT ?,coalesce(max(sequence),0) "
                "FROM graph_events WHERE graph=? ON CONFLICT(graph) DO UPDATE SET applied_sequence=excluded.applied_sequence",
                (self.name, self.name),
            )

    def reset(self) -> None:
        with self.transaction(deleting=True):
            database = self._db()
            database.execute("DELETE FROM graph_nodes WHERE graph=?", (self.name,))
            database.execute("DELETE FROM graph_events WHERE graph=?", (self.name,))
            database.execute("DELETE FROM graph_consumers WHERE graph=?", (self.name,))

    def event_stats(self) -> dict[str, int]:
        with self.transaction(write=False):
            row: sqlite3.Row | None = (
                self._db()
                .execute(
                    "SELECT count(*),coalesce(sum(length(cast(payload AS BLOB))),0) FROM graph_events WHERE graph=?",
                    (self.name,),
                )
                .fetchone()
            )
            events: object = None
            size: object = None
            if row is not None:
                events = row[0]
                size = row[1]
            if type(events) is not int or type(size) is not int:
                raise OSError("invalid Ghost graph event statistics")
            return {"events": events, "bytes": size}
