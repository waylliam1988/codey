"""Atomic, idempotent knowledge commits in the same SQLite authority as notes.

Commit records contain completed knowledge versions, never execution positions.
The operation/effect system owns admission, retries and recovery.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from collections.abc import Sequence

from codey.knowledge.index import KnowledgeIndex
from codey.knowledge.model import KnowledgeMutation, KnowledgeObject, KnowledgeVersion, canonical_json
from codey.knowledge.note import now_iso
from codey.knowledge.search_text import SEARCH_INDEX_VERSION, indexed_text

# Relation direction is explicit: a claim is supported_by evidence, an evidence
# fragment is from_source a source version. Co-occurrence is not a fact relation.
_TARGET_KINDS = {
    "from_source": {"source"}, "about": {"entity"},
    "supported_by": {"evidence", "observation"}, "refuted_by": {"evidence", "observation"},
    "limited_by": {"evidence", "observation"},
    "depends_on": {"claim", "inference", "evidence", "observation"},
    "uses": {"claim", "inference", "evidence", "source", "observation", "question", "entity"},
    "motivated_by": {"claim", "inference", "evidence", "observation", "question"},
    "supersedes": {"claim", "inference", "source"}, "same_study": {"source"},
}
_SOURCE_KINDS = {
    "from_source": {"evidence"}, "about": {"claim", "inference", "question", "observation", "evidence"},
    "supported_by": {"claim"}, "refuted_by": {"claim"}, "limited_by": {"claim"},
    "depends_on": {"inference"}, "uses": {"answer"},
    "motivated_by": {"question"}, "supersedes": {"claim", "inference", "source"}, "same_study": {"source"},
}


class KnowledgeRepository:
    def __init__(self, index: KnowledgeIndex) -> None:
        self.index = index
        with index.transaction() as conn:
            for sql in (
                "CREATE TABLE IF NOT EXISTS knowledge_meta(key TEXT PRIMARY KEY,value TEXT NOT NULL)",
                "CREATE TABLE IF NOT EXISTS knowledge_heads(id TEXT PRIMARY KEY,revision INTEGER NOT NULL)",
                "CREATE TABLE IF NOT EXISTS knowledge_versions(id TEXT NOT NULL,revision INTEGER NOT NULL,"
                "kind TEXT NOT NULL,scope TEXT NOT NULL,scope_id TEXT NOT NULL,status TEXT NOT NULL,"
                "payload TEXT NOT NULL,recorded_at TEXT NOT NULL,commit_id TEXT NOT NULL,PRIMARY KEY(id,revision))",
                "CREATE TABLE IF NOT EXISTS knowledge_commits(id TEXT PRIMARY KEY,digest TEXT NOT NULL,"
                "result TEXT NOT NULL,recorded_at TEXT NOT NULL)",
                "CREATE TABLE IF NOT EXISTS knowledge_commit_context(commit_id TEXT NOT NULL,session_id TEXT NOT NULL,"
                "project TEXT NOT NULL,PRIMARY KEY(commit_id,session_id,project))",
                "CREATE TABLE IF NOT EXISTS knowledge_purged_commits(id TEXT PRIMARY KEY)",
                "CREATE TABLE IF NOT EXISTS knowledge_refs(id TEXT NOT NULL,revision INTEGER NOT NULL,"
                "target TEXT NOT NULL,target_revision INTEGER NOT NULL,relation TEXT NOT NULL,"
                "PRIMARY KEY(id,revision,target,target_revision,relation))",
                "CREATE TABLE IF NOT EXISTS knowledge_aliases(id TEXT NOT NULL,alias TEXT NOT NULL,PRIMARY KEY(id,alias))",
                "CREATE INDEX IF NOT EXISTS idx_knowledge_target ON knowledge_refs(target,target_revision)",
                "CREATE INDEX IF NOT EXISTS idx_knowledge_scope ON knowledge_versions(scope,scope_id)",
                "CREATE INDEX IF NOT EXISTS idx_knowledge_alias ON knowledge_aliases(alias)",
            ):
                conn.execute(sql)
            if index.fts_enabled:
                conn.execute("CREATE VIRTUAL TABLE IF NOT EXISTS knowledge_fts USING fts5("
                             "id UNINDEXED,title,content,aliases,tokenize='unicode61')")
                version = conn.execute("SELECT value FROM knowledge_meta WHERE key='structured_fts_version'").fetchone()
                if version is None or version[0] != SEARCH_INDEX_VERSION:
                    self.rebuild()

    def read(self, identity: str, revision: int | None = None) -> KnowledgeVersion | None:
        conn = self.index._read_conn()
        if revision is None:
            row = conn.execute("SELECT v.* FROM knowledge_versions v JOIN knowledge_heads h "
                               "ON v.id=h.id AND v.revision=h.revision WHERE v.id=?", (identity,)).fetchone()
        else:
            row = conn.execute("SELECT * FROM knowledge_versions WHERE id=? AND revision=?", (identity, revision)).fetchone()
        if row is None:
            return None
        value = KnowledgeObject.from_dict(json.loads(row["payload"]))
        changed = conn.execute(
            "WITH RECURSIVE dependencies(id,revision) AS (VALUES(?,?) UNION "
            "SELECT r.target,r.target_revision FROM knowledge_refs r JOIN dependencies d "
            "ON r.id=d.id AND r.revision=d.revision LIMIT 257) "
            "SELECT 1 FROM dependencies d LEFT JOIN knowledge_heads h ON h.id=d.id "
            "LEFT JOIN knowledge_versions v ON v.id=h.id AND v.revision=h.revision "
            "WHERE h.revision IS NULL OR h.revision!=d.revision OR v.status IN ('withdrawn','stale','contradicted','superseded') "
            "OR (SELECT COUNT(*) FROM dependencies)>=257 LIMIT 1",
            (identity, row["revision"]),
        ).fetchone()
        return KnowledgeVersion(value, int(row["revision"]), str(row["recorded_at"]), str(row["commit_id"]), bool(changed))

    def commit(self, commit_id: str, mutations: Sequence[KnowledgeMutation], *, shared_staged: bool = False) -> dict[str, int]:
        if not commit_id.strip() or len(commit_id) > 512 or not mutations or len(mutations) > 128:
            raise ValueError("a bounded nonempty commit and stable commit_id are required")
        identities = [mutation.value.id for mutation in mutations]
        if len(identities) != len(set(identities)):
            raise ValueError("duplicate identity in knowledge commit")
        digest = self.batch_digest(mutations)
        with self.index.transaction() as conn:
            self._check_not_purged(commit_id)
            old_commit = conn.execute("SELECT digest,result FROM knowledge_commits WHERE id=?", (commit_id,)).fetchone()
            if old_commit:
                if old_commit["digest"] != digest:
                    raise ValueError("knowledge commit identity reused with different content")
                result: dict[str, int] = json.loads(old_commit["result"])
                return result
            versions = self._check_revisions(mutations, shared_staged=shared_staged)
            self._check_cycles(versions)
            for mutation in mutations:
                self._validate_references(mutation.value, versions)
            stamp = now_iso()
            result = {}
            for mutation in mutations:
                value, revision = versions[mutation.value.id]
                result[value.id] = revision
                if conn.execute("SELECT 1 FROM knowledge_versions WHERE id=? AND revision=?", (value.id, revision)).fetchone() is None:
                    self._write(conn, value, revision, stamp, commit_id)
            conn.execute("INSERT INTO knowledge_commits VALUES(?,?,?,?)", (commit_id, digest, canonical_json(result), stamp))
            return result

    @staticmethod
    def batch_digest(mutations: Sequence[KnowledgeMutation]) -> str:
        return hashlib.sha256(canonical_json([
            {"value": m.value.to_dict(), "expected_revision": m.expected_revision} for m in mutations
        ]).encode()).hexdigest()

    def committed(self, commit_id: str, mutations: Sequence[KnowledgeMutation]) -> dict[str, int] | None:
        self._check_not_purged(commit_id)
        row = self.index._read_conn().execute("SELECT digest,result FROM knowledge_commits WHERE id=?", (commit_id,)).fetchone()
        if row is None:
            return None
        if row["digest"] != self.batch_digest(mutations):
            raise ValueError("knowledge commit identity reused with different content")
        result: dict[str, int] = json.loads(row["result"])
        return result

    def _check_not_purged(self, commit_id: str) -> None:
        if self.index._read_conn().execute("SELECT 1 FROM knowledge_purged_commits WHERE id=?", (commit_id,)).fetchone():
            raise ValueError("knowledge commit contains purged content and cannot be replayed")

    def _check_revisions(self, mutations: Sequence[KnowledgeMutation], *, shared_staged: bool = False) -> dict[str, tuple[KnowledgeObject, int]]:
        versions: dict[str, tuple[KnowledgeObject, int]] = {}
        for mutation in mutations:
            value = mutation.value
            old = self.read(value.id)
            if (shared_staged and mutation.expected_revision == 0 and old and old.revision == 1
                    and value.kind in {"source", "evidence", "entity"} and old.value == value):
                versions[value.id] = (value, 1)
                continue
            if (old.revision if old else 0) != mutation.expected_revision:
                raise ValueError(f"knowledge revision conflict: {value.id}")
            if old and (old.value.kind, old.value.scope, old.value.scope_id) != (value.kind, value.scope, value.scope_id):
                raise ValueError("knowledge kind and scope are immutable; create a new identity")
            versions[value.id] = (value, mutation.expected_revision + 1)
        return versions

    @staticmethod
    def _check_cycles(versions: dict[str, tuple[KnowledgeObject, int]]) -> None:
        visiting: set[str] = set()
        visited: set[str] = set()

        def visit(identity: str) -> None:
            if identity in visiting:
                raise ValueError("cyclic knowledge premises")
            if identity in visited:
                return
            visiting.add(identity)
            for ref in versions[identity][0].references:
                if ref.target in versions and versions[ref.target][1] == ref.revision:
                    visit(ref.target)
            visiting.remove(identity)
            visited.add(identity)

        for identity in versions:
            visit(identity)

    def _validate_references(self, value: KnowledgeObject, versions: dict[str, tuple[KnowledgeObject, int]]) -> None:
        for ref in value.references:
            pending = versions.get(ref.target)
            persisted = self.read(ref.target, ref.revision)
            target = pending[0] if pending and pending[1] == ref.revision else persisted.value if persisted else None
            if target is None or ref.target == value.id:
                raise ValueError("missing or self-referential knowledge version")
            if (value.kind not in _SOURCE_KINDS.get(ref.relation, set())
                    or target.kind not in _TARGET_KINDS.get(ref.relation, set())):
                raise ValueError(f"invalid evidence relation: {value.kind} {ref.relation} {target.kind}")
            if target.scope != "global" and (target.scope, target.scope_id) != (value.scope, value.scope_id):
                raise ValueError("knowledge relation would expose a restricted scope")
            if value.kind == "evidence" and ref.relation == "from_source" and value.content not in target.content:
                raise ValueError("evidence excerpt does not occur in the pinned source version")
        relations = {ref.relation for ref in value.references}
        if value.kind == "evidence" and "from_source" not in relations:
            raise ValueError("evidence requires an exact source version")
        if value.kind == "source" and not value.provenance.get("url"):
            raise ValueError("source requires a provenance URL")
        if value.kind == "claim" and value.status == "active" and "supported_by" not in relations:
            raise ValueError("active claim requires supporting evidence; keep unsupported claims as candidates")
        if value.kind == "claim" and value.origin == "model_extraction" and value.status == "active":
            raise ValueError("model-extracted claims are unreviewed; keep them as candidates")
        if value.kind == "inference" and not relations.intersection({"depends_on"}):
            raise ValueError("inference requires pinned premises")

    def _write(self, conn: sqlite3.Connection, value: KnowledgeObject, revision: int, stamp: str, commit_id: str) -> None:
        conn.execute("INSERT INTO knowledge_versions VALUES(?,?,?,?,?,?,?,?,?)",
                     (value.id, revision, value.kind, value.scope, value.scope_id, value.status,
                      canonical_json(value.to_dict()), stamp, commit_id))
        conn.execute("INSERT INTO knowledge_heads VALUES(?,?) ON CONFLICT(id) DO UPDATE SET revision=excluded.revision",
                     (value.id, revision))
        conn.executemany("INSERT INTO knowledge_refs VALUES(?,?,?,?,?)",
                         [(value.id, revision, r.target, r.revision, r.relation) for r in value.references])
        conn.execute("DELETE FROM knowledge_aliases WHERE id=?", (value.id,))
        conn.executemany("INSERT OR IGNORE INTO knowledge_aliases VALUES(?,?)",
                         [(value.id, alias.casefold()) for alias in (*value.aliases, value.title) if alias])
        if self.index.fts_enabled:
            conn.execute("DELETE FROM knowledge_fts WHERE id=?", (value.id,))
            conn.execute("INSERT INTO knowledge_fts VALUES(?,?,?,?)",
                         (value.id, indexed_text(value.title), indexed_text(value.content),
                          indexed_text(" ".join(value.aliases))))

    def rebuild(self) -> None:
        with self.index.transaction() as conn:
            conn.execute("DELETE FROM knowledge_aliases")
            if self.index.fts_enabled:
                conn.execute("DELETE FROM knowledge_fts")
            rows = conn.execute("SELECT v.* FROM knowledge_versions v JOIN knowledge_heads h "
                                "ON v.id=h.id AND v.revision=h.revision").fetchall()
            for row in rows:
                value = KnowledgeObject.from_dict(json.loads(row["payload"]))
                conn.executemany("INSERT OR IGNORE INTO knowledge_aliases VALUES(?,?)",
                                 [(value.id, a.casefold()) for a in (*value.aliases, value.title) if a])
                if self.index.fts_enabled:
                    conn.execute("INSERT INTO knowledge_fts VALUES(?,?,?,?)",
                                 (value.id, indexed_text(value.title), indexed_text(value.content),
                                  indexed_text(" ".join(value.aliases))))
            if self.index.fts_enabled:
                conn.execute("INSERT INTO knowledge_meta VALUES('structured_fts_version',?) "
                             "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (SEARCH_INDEX_VERSION,))

    def purge(self, identities: Sequence[str]) -> set[str]:
        """Purge requested content and dependent revisions, preserving no private payload.

        The caller also removes managed artifacts/exports. Durable execution
        receipts have their own retention boundary and are not rewritten here.
        """
        removed = set(identities)
        with self.index.transaction() as conn:
            while True:
                more = {str(row[0]) for target in removed for row in conn.execute(
                    "SELECT DISTINCT id FROM knowledge_refs WHERE target=?", (target,)
                )} - removed
                if not more:
                    break
                removed.update(more)
            for identity in removed:
                conn.execute("INSERT OR IGNORE INTO knowledge_purged_commits SELECT commit_id FROM knowledge_versions WHERE id=?", (identity,))
                conn.execute("DELETE FROM knowledge_commit_context WHERE commit_id IN (SELECT id FROM knowledge_purged_commits)")
                conn.execute("DELETE FROM knowledge_refs WHERE id=? OR target=?", (identity, identity))
                conn.execute("DELETE FROM knowledge_versions WHERE id=?", (identity,))
                conn.execute("DELETE FROM knowledge_heads WHERE id=?", (identity,))
                conn.execute("DELETE FROM knowledge_aliases WHERE id=?", (identity,))
                if self.index.fts_enabled:
                    conn.execute("DELETE FROM knowledge_fts WHERE id=?", (identity,))
        return removed
