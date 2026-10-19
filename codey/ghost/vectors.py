"""Native sqlite-vec ranking of task observations in the independent Ghost DB."""

from __future__ import annotations

import hashlib
import importlib
import sqlite3
from collections.abc import Mapping

from codey.ghost.embeddings import Embeddings, pack_embedding
from codey.ghost.sqlite import GhostSQLite


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
            found = connection.execute(
                "SELECT o.run_id, e.content_digest, vec_distance_cosine(e.vector, ?) AS distance "
                "FROM memory_embeddings e JOIN observation_events o ON o.run_id=e.run_id "
                f"WHERE e.model_id=? AND e.dimensions=? AND o.committed=1 AND o.run_id IN ({placeholders}) "
                "ORDER BY distance, o.sequence DESC LIMIT ?",
                (vector, self.embeddings.model_id, self.embeddings.dimensions, *run_ids, min(200, limit)),
            ).fetchall()
        by_id = {str(row["run_id"]): row for row in rows}
        return tuple(by_id[str(row["run_id"])] for row in found
                     if str(row["content_digest"]) == observation_digest(by_id[str(row["run_id"])]))

    @staticmethod
    def _load_extension(connection: sqlite3.Connection) -> None:
        extension = importlib.import_module("sqlite_vec")
        connection.enable_load_extension(True)
        try:
            extension.load(connection)
        finally:
            connection.enable_load_extension(False)

    def _index(self, rows: tuple[dict[str, object], ...]) -> None:
        with self.database.connection() as connection:
            cached = {
                str(row["run_id"]): (str(row["model_id"]), int(row["dimensions"]), str(row["content_digest"]))
                for row in connection.execute("SELECT run_id, model_id, dimensions, content_digest FROM memory_embeddings")
            }
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
                for row, vector in zip(batch, packed, strict=True):
                    connection.execute(
                        "INSERT INTO memory_embeddings(run_id, model_id, dimensions, content_digest, vector) "
                        "SELECT run_id, ?, ?, ?, ? FROM observation_events "
                        "WHERE run_id=? AND committed=1 AND user_text=? AND assistant_text=? "
                        "ON CONFLICT(run_id) DO UPDATE SET model_id=excluded.model_id, dimensions=excluded.dimensions, "
                        "content_digest=excluded.content_digest, vector=excluded.vector",
                        (self.embeddings.model_id, self.embeddings.dimensions, observation_digest(row), vector,
                         row["run_id"], row["user_text"], row["assistant_text"]),
                    )
