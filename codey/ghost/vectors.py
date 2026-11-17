"""Native sqlite-vec ranking of task observations in the independent Ghost DB."""

from __future__ import annotations

import hashlib
import importlib
import sqlite3
from collections.abc import Iterable, Mapping
from typing import Protocol, runtime_checkable

from codey.ghost.embeddings import Embeddings, pack_embedding
from codey.ghost.event_windows import event_window
from codey.ghost.sqlite import GhostSQLite


@runtime_checkable
class _VectorExtension(Protocol):
    def load(self, connection: sqlite3.Connection) -> None: ...


def observation_text(row: Mapping[str, object]) -> str:
    return str(row["user_text"]) + "\n\n" + str(row["assistant_text"])


def observation_digest(row: Mapping[str, object]) -> str:
    return hashlib.sha256(observation_text(row).encode("utf-8")).hexdigest()


class GhostVectorMemory:
    def __init__(self, database: GhostSQLite, embeddings: Embeddings) -> None:
        self.database = database
        self.embeddings = embeddings

    def rank(self, query: str, rows: tuple[dict[str, object], ...], *, limit: int) -> tuple[dict[str, object], ...]:
        if not query.strip() or not rows or limit <= 0:
            return ()
        self._index(rows)
        encoded = self.embeddings.encode((query,))
        if len(encoded) != 1:
            raise ValueError("embedding response cardinality mismatch")
        vector = pack_embedding(encoded[0], self.embeddings.dimensions)
        run_ids = tuple(str(row["run_id"]) for row in rows)
        placeholders = ",".join("?" for _run in run_ids)
        with self.database.connection() as connection:
            self._load_extension(connection)
            event_window(connection, "observations")
            found: list[sqlite3.Row] = connection.execute(
                "SELECT o.run_id, e.content_digest, vec_distance_cosine(e.vector, ?) AS distance "
                "FROM memory_embeddings e JOIN observation_history o ON o.run_id=e.run_id "
                f"WHERE e.model_id=? AND e.dimensions=? AND o.committed=1 AND o.run_id IN ({placeholders}) "
                "AND NOT EXISTS (SELECT 1 FROM ghost_retired_inputs WHERE kind='observation' AND identity=o.run_id) "
                "AND codey_input_receipt_ok((SELECT database_name FROM ghost_input_authority WHERE singleton=1),o.run_id) "
                "AND o.sequence>(SELECT after_sequence FROM ghost_replay_windows WHERE stream='observations') "
                "ORDER BY distance, o.sequence DESC LIMIT ?",
                (vector, self.embeddings.model_id, self.embeddings.dimensions, *run_ids, min(200, limit)),
            ).fetchall()
        by_id = {str(row["run_id"]): row for row in rows}
        ranked: list[dict[str, object]] = []
        for match in found:
            identity: object = match["run_id"]
            digest: object = match["content_digest"]
            if not isinstance(identity, str) or not isinstance(digest, str):
                raise ValueError("invalid Ghost vector ranking row")
            candidate = by_id.get(identity)
            if candidate is not None and digest == observation_digest(candidate):
                ranked.append(candidate)
        return tuple(ranked)

    @staticmethod
    def _load_extension(connection: sqlite3.Connection) -> None:
        extension: object = importlib.import_module("sqlite_vec")
        if not isinstance(extension, _VectorExtension):
            raise TypeError("sqlite_vec must expose its connection loader")
        connection.enable_load_extension(True)
        try:
            extension.load(connection)
        finally:
            connection.enable_load_extension(False)

    def _index(self, rows: tuple[dict[str, object], ...]) -> None:
        with self.database.connection() as connection:
            cached: dict[str, tuple[str, int, str]] = {}
            cache_rows: Iterable[sqlite3.Row] = connection.execute(
                "SELECT run_id, model_id, dimensions, content_digest FROM memory_embeddings",
            )
            for stored in cache_rows:
                identity: object = stored["run_id"]
                model_id: object = stored["model_id"]
                dimensions: object = stored["dimensions"]
                digest: object = stored["content_digest"]
                if (not isinstance(identity, str) or not isinstance(model_id, str) or type(dimensions) is not int
                        or not isinstance(digest, str)):
                    raise ValueError("invalid Ghost vector cache row")
                cached[identity] = model_id, dimensions, digest
        missing = [row for row in rows if cached.get(str(row["run_id"])) != (
            self.embeddings.model_id, self.embeddings.dimensions, observation_digest(row),
        )]
        for start in range(0, len(missing), 16):
            batch = missing[start:start + 16]
            encoded = self.embeddings.encode(tuple(observation_text(row) for row in batch))
            if len(encoded) != len(batch):
                raise ValueError("embedding response cardinality mismatch")
            packed = tuple(pack_embedding(vector, self.embeddings.dimensions) for vector in encoded)
            # Provider I/O and validation finish before the SQLite write lock.
            with self.database.connection() as connection:
                event_window(connection, "observations")
                for row, vector in zip(batch, packed, strict=True):
                    connection.execute(
                        "INSERT INTO memory_embeddings(run_id, model_id, dimensions, content_digest, vector) "
                        "SELECT run_id, ?, ?, ?, ? FROM observation_history "
                        "WHERE run_id=? AND committed=1 AND user_text=? AND assistant_text=? "
                        "AND NOT EXISTS (SELECT 1 FROM ghost_retired_inputs "
                        "WHERE kind='observation' AND identity=observation_history.run_id) "
                        "AND codey_input_receipt_ok((SELECT database_name FROM ghost_input_authority WHERE singleton=1),run_id) "
                        "AND sequence>(SELECT after_sequence FROM ghost_replay_windows WHERE stream='observations') "
                        "ON CONFLICT(run_id) DO UPDATE SET model_id=excluded.model_id, dimensions=excluded.dimensions, "
                        "content_digest=excluded.content_digest, vector=excluded.vector",
                        (self.embeddings.model_id, self.embeddings.dimensions, observation_digest(row), vector,
                         row["run_id"], row["user_text"], row["assistant_text"]),
                    )
