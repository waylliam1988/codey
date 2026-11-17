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
                decoded_object(row[0])
                for row in rows
            ]

    def rows(self) -> tuple[list[dict[str, object]], list[dict[str, object]]]:
        with self.transaction(write=False):
            database = self._db()
            self._check_cursor()
            node_rows: Iterable[sqlite3.Row] = database.execute(
                "SELECT payload FROM graph_nodes WHERE graph=? ORDER BY id", (self.name,),
            )
            nodes = [decoded_object(row[0]) for row in node_rows]
            edge_rows: Iterable[sqlite3.Row] = database.execute(
                "SELECT payload FROM graph_edges WHERE graph=? ORDER BY source,target,relation", (self.name,),
            )
            edges = [decoded_object(row[0]) for row in edge_rows]
            return nodes, edges

    def _check_cursor(self) -> None:
        database = self._db()
        tail_row: sqlite3.Row | None = database.execute(
            "SELECT coalesce(max(sequence),0) FROM graph_events WHERE graph=?", (self.name,)
        ).fetchone()
        tail: object = tail_row[0] if tail_row is not None else None
        cursor: sqlite3.Row | None = database.execute(
            "SELECT applied_sequence FROM graph_consumers WHERE graph=?", (self.name,),
        ).fetchone()
        position: object = cursor[0] if cursor is not None else 0
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
                    if existing[0] != payload:
                        raise ValueError("Ghost graph event identity conflict")
                    continue
                database.execute(
                    "INSERT INTO graph_events(graph,event_id,payload) VALUES(?,?,?)", (self.name, identity, payload)
                )

    def replace_rows(self, nodes: Iterable[Mapping[str, object]], edges: Iterable[Mapping[str, object]]) -> None:
        with self.transaction():
            database = self._db()
            database.execute("DELETE FROM graph_nodes WHERE graph=?", (self.name,))
            for node in nodes:
                database.execute(
                    "INSERT INTO graph_nodes(graph,id,kind,scope,scope_ref,status,weight,payload) VALUES(?,?,?,?,?,?,?,?)",
                    (
                        self.name,
                        node["id"],
                        node["kind"],
                        node["scope"],
                        node["scope_ref"],
                        node["status"],
                        node["weight"],
                        _encode(node),
                    ),
                )
            for edge in edges:
                database.execute(
                    "INSERT INTO graph_edges(graph,source,target,relation,weight,payload) VALUES(?,?,?,?,?,?)",
                    (self.name, edge["source"], edge["target"], edge["relation"], edge["weight"], _encode(edge)),
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
            events: object = row[0] if row is not None else None
            size: object = row[1] if row is not None else None
            if type(events) is not int or type(size) is not int:
                raise OSError("invalid Ghost graph event statistics")
            return {"events": events, "bytes": size}
