"""Knowledge proposals and their atomic commit/rollback over the original store."""
from __future__ import annotations

from copy import deepcopy
from dataclasses import replace
from pathlib import Path

from codey.knowledge.changes import KnowledgeChanges
from codey.knowledge.index import KnowledgeIndex
from codey.knowledge.model import KnowledgeMutation, KnowledgeVersion, canonical_json
from codey.knowledge.note import LINK_KINDS, KnowledgeNote
from codey.knowledge.recall import KnowledgeHit
from codey.knowledge.repository import KnowledgeRepository
from codey.knowledge.store import KnowledgeLinkResult, KnowledgeStore
from codey.types.json_value import JsonObject, canonical_json_object


class StagedKnowledgeStore:
    """In-memory staging buffer over KnowledgeStore that defers disk writes until committed."""

    def __init__(self, parent_store: KnowledgeStore) -> None:
        self._parent = parent_store
        self._staged_notes: dict[str, KnowledgeNote] = {}
        self._staged_links: list[tuple[str, str, str]] = []
        self._staged_knowledge: dict[str, tuple[str, list[KnowledgeMutation]]] = {}
        self._staged_bases: dict[str, str | None] = {}

    @property
    def root(self) -> Path:
        return self._parent.root

    @property
    def index(self) -> KnowledgeIndex:
        return self._parent.index

    @property
    def repository(self) -> KnowledgeRepository:
        return self._parent.repository

    @property
    def owner_id(self) -> str:
        return self._parent.owner_id

    def search(self, query: str, *, project: str = "", session_id: str = "", limit: int = 8,
               depth: int = 1, exclude_kinds: tuple[str, ...] = (),
               exclude_ids: tuple[str, ...] = ()) -> list[KnowledgeHit]:
        return self._parent.search(query, project=project, session_id=session_id, limit=limit,
                                   depth=depth, exclude_kinds=exclude_kinds, exclude_ids=exclude_ids)

    def search_notes(self, query: str, *, project: str = "", session_id: str = "") -> list[JsonObject]:
        return [canonical_json_object(row) for row in
                self._parent.search_notes(query, project=project, session_id=session_id)]

    def note_references(self, note_id: str, revision: int | None = None) -> list[JsonObject]:
        return [canonical_json_object(row) for row in self._parent.note_references(note_id, revision)]

    def read_version(self, identity: str, revision: int | None = None) -> KnowledgeVersion | None:
        """Read a prospective staged version without exposing the mutable buffer."""
        for commit_id, mutations in self._staged_knowledge.values():
            for mutation in mutations:
                if mutation.value.id == identity and revision in {None, mutation.expected_revision + 1}:
                    return KnowledgeVersion(deepcopy(mutation.value), mutation.expected_revision + 1, "", commit_id)
        return self.repository.read(identity, revision)

    def publish_note(self, note: KnowledgeNote, commit_id: str, mutations: list[KnowledgeMutation],
                     *, changes: KnowledgeChanges | None = None) -> str:
        previous = self._staged_knowledge.get(note.id)
        if previous is not None:
            from codey.knowledge.archive import stable_identity

            merged = {mutation.value.id: mutation for mutation in previous[1]}
            for mutation in mutations:
                old = merged.get(mutation.value.id)
                if old is not None:
                    if old.expected_revision != mutation.expected_revision:
                        raise ValueError("staged knowledge changed its original revision")
                    value = replace(mutation.value, references=tuple(dict.fromkeys(
                        (*old.value.references, *mutation.value.references))))
                    mutation = KnowledgeMutation(value, mutation.expected_revision)
                merged[mutation.value.id] = mutation
            mutations = list(merged.values())
            commit_id = stable_identity("archive", note.id, canonical_json([
                {"value": m.value.to_dict(), "expected_revision": m.expected_revision} for m in mutations]))
        self._staged_knowledge[note.id] = (commit_id, deepcopy(mutations))
        return self.write_note(note, changes=changes)

    def path_for(self, note: KnowledgeNote) -> Path:
        return self._parent.path_for(note)

    def rel(self, path: Path) -> str:
        return self._parent.rel(path)

    def exists(self, note_id: str) -> bool:
        return note_id in self._staged_notes or self._parent.exists(note_id)

    def resolve(self, target: str) -> str | None:
        target = (target or "").strip()
        if not target:
            return None
        if target in self._staged_notes:
            return target
        parent_id = self._parent.index.resolve(target)
        if parent_id:
            return parent_id
        for note in self._staged_notes.values():
            if note.title == target:
                return note.id
        return None

    def read_note(self, note_id: str, *, revision: int | None = None) -> KnowledgeNote | None:
        if revision is None and note_id in self._staged_notes:
            return deepcopy(self._staged_notes[note_id])
        return self._parent.read_note(note_id, revision=revision)

    def write_note(
        self,
        note: KnowledgeNote,
        *,
        changes: KnowledgeChanges | None = None,
    ) -> str:
        if note.id not in self._staged_bases:
            original = self._parent.read_note(note.id)
            self._staged_bases[note.id] = original.to_markdown() if original else None
        self._staged_notes[note.id] = deepcopy(note)
        if changes is not None:
            record_staged = getattr(changes, "record_staged_note", None)
            if callable(record_staged):
                record_staged(note)
        path = self._parent.path_for(note)
        return self._parent.rel(path) if path.exists() else f"{note.folder}/{note.id}.md"

    def link(self, src: str, dst: str, kind: str = "relates", *, changes: KnowledgeChanges | None = None) -> KnowledgeLinkResult:
        del changes
        kind = kind if kind in LINK_KINDS else "relates"
        src_id = self.resolve(src)
        if src_id is None:
            return KnowledgeLinkResult(False, f"ERROR: unknown source note: {src}")
        dst_id = self.resolve(dst)
        if dst_id is None:
            return KnowledgeLinkResult(False, f"ERROR: unknown target note: {dst}")
        self._staged_links.append((src_id, dst_id, kind))
        return KnowledgeLinkResult(True, f"linked: {src_id} -> {dst_id} ({kind})", created=True)

    def commit_to(self, target_store: KnowledgeStore, changes: KnowledgeChanges | None = None) -> None:
        changes_snapshot = changes.snapshot() if changes is not None else None
        try:
            with target_store.index.transaction():
                for note in self._staged_notes.values():
                    batch = self._staged_knowledge.get(note.id)
                    original = target_store.read_note(note.id)
                    unchanged = original is not None and replace(original, updated=note.updated).to_markdown() == note.to_markdown()
                    if not unchanged and (original.to_markdown() if original else None) != self._staged_bases[note.id]:
                        raise ValueError("staged document revision conflict")
                    if batch is not None:
                        if unchanged and target_store.repository.committed(batch[0], batch[1]) is not None:
                            continue
                        target_store.publish_note(note, batch[0], batch[1], changes=changes, shared_staged=True)
                        continue
                    if unchanged:
                        continue
                    target_store.write_note(note, changes=changes)
                for src_id, dst_id, kind in self._staged_links:
                    result = target_store.link(src_id, dst_id, kind, changes=changes)
                    if not result.ok:
                        raise ValueError(result.model_text)
        except BaseException:
            if changes is not None and changes_snapshot is not None:
                changes.restore_snapshot(changes_snapshot)
            raise
        self._staged_notes.clear()
        self._staged_links.clear()
        self._staged_knowledge.clear()
        self._staged_bases.clear()


class StagedKnowledgeChanges:
    """In-memory tracking for note file changes during staging without disk side-effects."""

    def __init__(self, parent_changes: KnowledgeChanges | None = None) -> None:
        del parent_changes

    def record_staged_note(self, note: KnowledgeNote) -> None:
        del note

    def capture_before(self, rel: str, path: Path) -> None:
        """No-op during staging: staged notes do not inspect or snapshot disk paths."""
        del rel, path

    def record_after(self, rel: str, path: Path) -> None:
        del rel, path
