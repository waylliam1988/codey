"""SQLite knowledge authority with explicit Markdown interchange."""

from __future__ import annotations

import contextlib
import hashlib
import re
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from codey.knowledge.changes import KnowledgeChanges
from codey.knowledge.documents import KnowledgeDocuments
from codey.knowledge.index import KnowledgeIndex
from codey.knowledge.model import KnowledgeMutation, canonical_json
from codey.knowledge.note import LINK_KINDS, KnowledgeNote, is_safe_id, now_iso, wikilink
from codey.knowledge.recall import KnowledgeAccess, KnowledgeHit, KnowledgeRecall
from codey.knowledge.repository import KnowledgeRepository
from codey.storage.atomic_io import write_text_atomic

RELATED_HEADING = "## Related"


@dataclass(frozen=True)
class KnowledgeLinkResult:
    ok: bool
    model_text: str
    created: bool = False


class KnowledgeStore:
    def __init__(self, root: str | Path) -> None:
        self.root = Path(root).expanduser().resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.index = KnowledgeIndex(self.root / ".codey" / "knowledge.db")
        self.documents = KnowledgeDocuments(self.index)
        self.repository = KnowledgeRepository(self.index)
        self._import_legacy_once()

    def path_for(self, note: KnowledgeNote) -> Path:
        if not is_safe_id(note.id):
            raise ValueError(f"unsafe note id: {note.id!r}")
        path = (self.root / note.folder / f"{note.id}.md").resolve()
        if path != self.root and self.root not in path.parents:
            raise ValueError(f"note path escapes the knowledge root: {note.id!r}")
        return path

    def rel(self, path: Path) -> str:
        return path.resolve().relative_to(self.root).as_posix()

    def exists(self, note_id: str) -> bool:
        return self.documents.get(note_id) is not None

    def search(self, query: str, *, project: str = "", session_id: str = "",
               limit: int = 8, depth: int = 1) -> list[KnowledgeHit]:
        return KnowledgeRecall(self.repository).search(query, access=KnowledgeAccess(project, session_id),
                                                       limit=limit, depth=depth)

    def search_notes(self, query: str, *, project: str = "", session_id: str = "") -> list[dict[str, Any]]:
        rows = self.index.search(query, limit=32)
        return [row for row in rows if (
            row.get("project") == project if row.get("project") else
             not row.get("session_id") or row.get("session_id") == session_id
        )][:8]

    def write_note(
        self,
        note: KnowledgeNote,
        *,
        changes: KnowledgeChanges | None = None,
    ) -> str:
        rel = self.rel(self.path_for(note))
        previous = self.documents.get(note.id)
        if previous is not None:
            from dataclasses import replace

            if replace(note, updated=previous[0].updated).to_markdown() == previous[0].to_markdown():
                note.updated = previous[0].updated
                return rel
        note.updated = now_iso()
        with self.index.transaction():
            if changes is not None:
                changes.capture_content(rel, previous[0].to_markdown() if previous else None)
                if previous and previous[1] != rel:
                    changes.capture_content(previous[1], previous[0].to_markdown())
            self.documents.write(note, rel)
            self._index_body_links(note)
            if changes is not None:
                changes.record_content(rel, note.to_markdown())
                if previous and previous[1] != rel:
                    changes.record_content(previous[1], None)
        return rel

    def link(
        self,
        src_target: str,
        dst_target: str,
        kind: str = "relates",
        *,
        changes: KnowledgeChanges | None = None,
    ) -> KnowledgeLinkResult:
        kind = kind if kind in LINK_KINDS else "relates"
        src_id = self.index.resolve(src_target)
        if src_id is None:
            return KnowledgeLinkResult(False, f"ERROR: unknown source note: {src_target}")
        src = self.read_note(src_id)
        if src is None:
            return KnowledgeLinkResult(False, f"ERROR: unknown source note: {src_target}")
        dst_id = self.index.resolve(dst_target)
        if dst_id is None:
            return KnowledgeLinkResult(False, f"ERROR: unknown target note: {dst_target}")
        dst = self.read_note(dst_id)
        display = dst.title or dst.id if dst else dst_id
        link_text = wikilink(display)
        if link_text in src.body:
            upgraded = _annotate_wikilink(src.body, link_text, kind)
            if upgraded is not None:
                src.body = upgraded
                self.write_note(src, changes=changes)
                self.index.add_link(src_id, dst_id, kind)
                return KnowledgeLinkResult(True, f"updated link: {src_id} -> {dst_id} ({kind})")
            self.index.add_link(src_id, dst_id, kind)
            return KnowledgeLinkResult(True, f"already linked: {src_id} -> {dst_id} ({kind})")
        src.body = _append_related(src.body, f"{link_text} ({kind})")
        self.write_note(src, changes=changes)
        self.index.add_link(src_id, dst_id, kind)
        return KnowledgeLinkResult(True, f"linked: {src_id} -> {dst_id} ({kind})", created=True)

    def publish_note(self, note: KnowledgeNote, commit_id: str, mutations: list[KnowledgeMutation],
                     *, changes: KnowledgeChanges | None = None, shared_staged: bool = False) -> str:
        snapshot = changes.snapshot() if changes is not None else None
        try:
            with self.index.transaction():
                if changes is not None:
                    for mutation in mutations:
                        previous = self.repository.read(mutation.value.id)
                        rel = f"_knowledge/{mutation.value.id}.json"
                        changes.capture_content(rel, canonical_json(previous.value.to_dict()) if previous else None)
                versions: dict[str, int] = self.repository.commit(commit_id, mutations, shared_staged=shared_staged) if mutations else {}
                rel = self.write_note(note, changes=changes)
                found = self.documents.get(note.id)
                if found:
                    self.index._read_conn().executemany("INSERT OR IGNORE INTO note_knowledge_refs VALUES(?,?,?,?)",
                        [(note.id, found[2], identity, revision) for identity, revision in versions.items()])
                if changes is not None:
                    for mutation in mutations:
                        changes.record_content(f"_knowledge/{mutation.value.id}.json", canonical_json(mutation.value.to_dict()))
                return rel
        except BaseException:
            if changes is not None and snapshot is not None:
                changes.restore_snapshot(snapshot)
            raise

    def note_references(self, note_id: str, revision: int | None = None) -> list[dict[str, Any]]:
        found = self.documents.get(note_id, revision)
        if not found:
            return []
        return [dict(row) for row in self.index._read_conn().execute(
            "SELECT target,revision FROM note_knowledge_refs WHERE note_id=? AND note_revision=?", (note_id, found[2]))]

    def read_note(self, note_id: str, *, revision: int | None = None) -> KnowledgeNote | None:
        found = self.documents.get(note_id, revision)
        return found[0] if found else None

    def read_notes(self, note_ids: list[str]) -> dict[str, KnowledgeNote]:
        return {note_id: note for note_id, (note, _row) in self.read_notes_with_rows(note_ids).items()}

    def read_notes_with_rows(
        self, note_ids: list[str]
    ) -> dict[str, tuple[KnowledgeNote, dict[str, Any]]]:
        out: dict[str, tuple[KnowledgeNote, dict[str, Any]]] = {}
        for note_id in dict.fromkeys(note_ids):
            found = self.documents.get(note_id)
            if found:
                note, path, revision = found
                out[note_id] = (note, {"id": note_id, "path": path, "revision": revision,
                                       "content_hash": _content_hash(note.to_markdown())})
        return out

    def iter_note_paths(self) -> Iterator[Path]:
        for path in sorted(self.root.rglob("*.md")):
            if ".codey" not in path.relative_to(self.root).parts:
                yield path

    def _import_legacy_once(self) -> None:
        if self.documents.initialized():
            return
        notes: list[tuple[KnowledgeNote, str]] = []
        seen: set[str] = set()
        for path in self.iter_note_paths():
            try:
                note = KnowledgeNote.from_markdown(path.read_text(encoding="utf-8"))
            except ValueError:
                continue
            self.path_for(note)  # Validate identity before accepting the migration.
            if note.id in seen:
                raise ValueError(f"duplicate legacy knowledge identity: {note.id}")
            seen.add(note.id)
            notes.append((note, self.rel(path)))
        # Source files and the old index.db are left untouched for inspection.
        with self.index.transaction():
            self.documents.initialize(notes)
            for note, _path in notes:
                self._index_body_links(note)

    def import_markdown(self, text: str, *, expected_revision: int | None = None) -> str:
        note = KnowledgeNote.from_markdown(text)
        with self.index.transaction():
            old = self.documents.get(note.id)
            current_revision = old[2] if old else 0
            if expected_revision != current_revision:
                raise ValueError(f"import revision conflict: expected {expected_revision}, current {current_revision}")
            return self.write_note(note)

    def export_note(self, note_id: str) -> Path:
        note = self.read_note(note_id)
        if note is None:
            raise ValueError(f"unknown note: {note_id}")
        path = self.path_for(note)
        write_text_atomic(path, note.to_markdown())
        return path

    def rebuild(self) -> int:
        with self.index.transaction():
            notes = self.documents.all()
            self.index.clear()
            for note, path, _version in notes:
                self.index.upsert(note, path=path, content_hash=_content_hash(note.to_markdown()))
            for note, _path, _version in notes:
                self._index_body_links(note)
            self.repository.rebuild()
        return len(notes)

    def delete_note(self, note_id: str, *, purge: bool = False) -> None:
        self.documents.remove(note_id, purge=purge)
        if purge:
            for path in self.iter_note_paths():
                try:
                    if KnowledgeNote.from_markdown(path.read_text(encoding="utf-8")).id == note_id:
                        path.unlink()
                except ValueError:
                    continue

    def close(self) -> None:
        self.index.close()

    def __del__(self) -> None:
        with contextlib.suppress(Exception):
            self.close()

    def _current_path(self, note_id: str) -> str | None:
        row = self.index.get(note_id)
        return (row.get("path") or None) if row else None

    def _index_body_links(self, note: KnowledgeNote) -> None:
        for target, kind in note.wikilink_edges():
            dst_id = self.index.resolve(target)
            if dst_id and dst_id != note.id:
                self.index.add_link(note.id, dst_id, kind)


def _append_related(body: str, entry: str) -> str:
    body = body.rstrip("\n")
    if RELATED_HEADING in body:
        return f"{body}\n{entry}\n"
    return f"{body}\n\n{RELATED_HEADING}\n{entry}\n"


def _annotate_wikilink(body: str, link_text: str, kind: str) -> str | None:
    kinds = "|".join(LINK_KINDS)
    pattern = re.compile(re.escape(link_text) + rf"(?:\s*\((?:{kinds})\))?")
    match = pattern.search(body)
    if match is None:
        return None
    desired = link_text if kind == "relates" else f"{link_text} ({kind})"
    if match.group(0) == desired:
        return None
    return body[: match.start()] + desired + body[match.end() :]


def content_hash_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()[:16]


def _content_hash(text: str) -> str:
    return content_hash_bytes(text.encode("utf-8"))
