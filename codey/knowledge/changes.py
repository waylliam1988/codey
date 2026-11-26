"""Per-run snapshots and restore for knowledge notes."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from codey.knowledge.change_snapshot import KnowledgeChangesSnapshot as KnowledgeChangesSnapshot
from codey.storage.atomic_io import write_text_atomic
from codey.workspace.changes import RestoreResult
from codey.workspace.paths import (
    content_hash as _content_hash,
)
from codey.workspace.paths import (
    path_hash as _path_hash,
)
from codey.workspace.paths import (
    read_text_or_none as _read_text_or_none,
)
from codey.workspace.paths import (
    safe_join as _safe_join,
)

MAX_SNAPSHOT_FILE_BYTES = 512 * 1024
MAX_SNAPSHOT_FILES = 200
MAX_SNAPSHOT_TOTAL_BYTES = 32 * 1024 * 1024


@dataclass
class KnowledgeChanges:
    root: Path
    _before: dict[str, str | None] = field(default_factory=dict)
    _after_hashes: dict[str, str] = field(default_factory=dict)
    _touched: set[str] = field(default_factory=set)
    last_restore_result: RestoreResult | None = None
    _document_revisions: dict[str, int] = field(default_factory=dict)

    def capture_content(self, rel: str, content: str | None, *, document_revision: int | None = None) -> None:
        _safe_join(self.root, rel, label="knowledge root")
        if rel not in self._before:
            self._validate_capacity(rel, content)
            self._before[rel] = content
            if document_revision is not None:
                self._document_revisions[rel] = document_revision

    def record_content(self, rel: str, content: str | None) -> None:
        self._touched.add(rel)
        self._after_hashes[rel] = _content_hash(content)


    def capture_before(self, rel: str, path: Path) -> None:
        path = _safe_join(self.root, rel, label="knowledge root")
        rel = path.relative_to(Path(self.root).expanduser().resolve()).as_posix()
        if rel in self._before:
            return
        before = _read_text_or_none(path, max_bytes=MAX_SNAPSHOT_FILE_BYTES)
        self._validate_capacity(rel, before)
        self._before[rel] = before

    def record_after(self, rel: str, path: Path) -> None:
        path = _safe_join(self.root, rel, label="knowledge root")
        rel = path.relative_to(Path(self.root).expanduser().resolve()).as_posix()
        self._touched.add(rel)
        try:
            self._after_hashes[rel] = _path_hash(path)
        except (OSError, UnicodeDecodeError, ValueError):
            self._after_hashes[rel] = "unknown"

    @property
    def created(self) -> list[str]:
        return sorted(r for r in self._touched if self._before.get(r) is None)

    @property
    def updated(self) -> list[str]:
        return sorted(r for r in self._touched if self._before.get(r) is not None)

    def summary(self) -> dict[str, list[str]]:
        return {"created": self.created, "updated": self.updated}

    def has_changes(self) -> bool:
        return bool(self._touched)

    def snapshot(self) -> KnowledgeChangesSnapshot:
        return KnowledgeChangesSnapshot(
            before=dict(self._before),
            after_hashes=dict(self._after_hashes),
            touched=frozenset(self._touched),
            last_restore_result=self.last_restore_result,
            document_revisions=dict(self._document_revisions),
        )

    def restore_snapshot(self, snapshot: KnowledgeChangesSnapshot) -> None:
        self._before = dict(snapshot.before)
        self._after_hashes = dict(snapshot.after_hashes)
        self._touched = set(snapshot.touched)
        self.last_restore_result = snapshot.last_restore_result
        self._document_revisions = dict(snapshot.document_revisions)

    def restore_result(self) -> RestoreResult:
        if (self.root / ".codey" / "knowledge.db").is_file():
            return self._restore_database()
        restored: list[str] = []
        conflicts: list[str] = []
        root = Path(self.root).expanduser().resolve()
        for rel in sorted(self._touched):
            try:
                path = _safe_join(root, rel, label="knowledge root")
                rel = path.relative_to(root).as_posix()
            except ValueError:
                conflicts.append(rel)
                continue
            original = self._before.get(rel)
            try:
                current_hash = _path_hash(path)
            except (OSError, UnicodeDecodeError, ValueError):
                conflicts.append(rel)
                continue
            if current_hash == _content_hash(original):
                self._forget(rel)
                continue
            expected_hash = self._after_hashes.get(rel)
            if expected_hash is None or expected_hash == "unknown" or current_hash != expected_hash:
                conflicts.append(rel)
                continue
            try:
                if original is None:
                    if path.is_file():
                        path.unlink()
                else:
                    path.parent.mkdir(parents=True, exist_ok=True)
                    write_text_atomic(path, original)
            except OSError:
                conflicts.append(rel)
                continue
            restored.append(rel)
            self._forget(rel)
        result = RestoreResult(
            ok=not conflicts,
            restored=restored,
            conflicts=conflicts,
            error=None if not conflicts else "restore conflict",
        )
        self.last_restore_result = result
        return result

    def _restore_database(self) -> RestoreResult:
        from dataclasses import replace

        from codey.knowledge.model import KnowledgeMutation, canonical_json
        from codey.knowledge.note import KnowledgeNote
        from codey.knowledge.repository import decode_knowledge_object
        from codey.knowledge.store import KnowledgeStore

        store = KnowledgeStore(self.root)
        restored: list[str] = []
        conflicts: list[str] = []
        plans: list[tuple[str, str | None]] = []
        mutations: list[KnowledgeMutation] = []
        try:
            with store.index.transaction():
                for rel in sorted(self._touched):
                    try:
                        _safe_join(self.root, rel, label="knowledge root")
                        if rel.startswith("_knowledge/"):
                            identity = Path(rel).stem
                            version = store.repository.read(identity)
                            current = None
                            if version is not None:
                                payload: object = version.value.to_dict()
                                current = canonical_json(payload)
                        else:
                            found = store.documents.by_path(rel)
                            current = found[0].to_markdown() if found else None
                        original = self._before.get(rel)
                        current_hash = _content_hash(current)
                        if current_hash != _content_hash(original):
                            if self._after_hashes.get(rel) != current_hash:
                                conflicts.append(rel)
                            else:
                                plans.append((rel, original))
                    except ValueError:
                        conflicts.append(rel)
                if not conflicts:
                    for rel, original in plans:
                        if rel.startswith("_knowledge/"):
                            version = store.repository.read(Path(rel).stem)
                            if version is not None:
                                value = (decode_knowledge_object(original) if original is not None
                                         else replace(version.value, status="withdrawn"))
                                mutations.append(KnowledgeMutation(value, version.revision))
                        elif original is None:
                            found = store.documents.by_path(rel)
                            if found:
                                store.delete_note(found[0].id)
                    for rel, original in plans:
                        if not rel.startswith("_knowledge/") and original is not None:
                            note = KnowledgeNote.from_markdown(original)
                            store.documents.write(note, rel, reason="compensation",
                                                  reference_revision=self._document_revisions.get(rel))
                            store._index_body_links(note)
                    if mutations:
                        digest = store.repository.batch_digest(mutations)
                        store.repository.commit("compensation:" + digest, mutations)
                    restored = [rel for rel, _original in plans]
        finally:
            store.close()
        if not conflicts:
            for rel in list(self._touched):
                self._forget(rel)
        result = RestoreResult(ok=not conflicts, restored=restored, conflicts=conflicts,
                               error="restore conflict" if conflicts else None)
        self.last_restore_result = result
        return result

    def _validate_capacity(self, rel: str, content: str | None) -> None:
        if rel not in self._before and len(self._before) >= MAX_SNAPSHOT_FILES:
            raise ValueError("snapshot file limit reached")
        total = sum(len((value or "").encode("utf-8")) for value in self._before.values())
        if rel not in self._before:
            total += len((content or "").encode("utf-8"))
        if total > MAX_SNAPSHOT_TOTAL_BYTES:
            raise ValueError("snapshot size limit reached")

    def _forget(self, rel: str) -> None:
        self._before.pop(rel, None)
        self._document_revisions.pop(rel, None)
        self._after_hashes.pop(rel, None)
        self._touched.discard(rel)
