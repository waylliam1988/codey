"""Markdown knowledge store with a rebuildable SQLite index."""

from __future__ import annotations

import hashlib
import os
import re
import uuid
from pathlib import Path

from codey.knowledge.changes import KnowledgeChanges
from codey.knowledge.index import KnowledgeIndex
from codey.knowledge.note import LINK_KINDS, KnowledgeNote, is_safe_id, now_iso, wikilink

RELATED_HEADING = "## Related"


class KnowledgeStore:
    def __init__(self, root: str | Path) -> None:
        self.root = Path(root).expanduser().resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.index = KnowledgeIndex(self.root / ".codey" / "index.db")

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
        return self.index.get(note_id) is not None

    def write_note(
        self,
        note: KnowledgeNote,
        *,
        changes: KnowledgeChanges | None = None,
    ) -> str:
        note.updated = now_iso()
        path = self.path_for(note)
        rel = self.rel(path) if path.exists() else (Path(note.folder) / f"{note.id}.md").as_posix()
        old_rel = self._current_path(note.id)
        moving = old_rel is not None and old_rel != rel
        if changes is not None:
            changes.capture_before(rel, path)
            if moving and old_rel:
                changes.capture_before(old_rel, self.root / old_rel)
        text = note.to_markdown()
        _atomic_write_text(path, text)
        self.index.upsert(note, path=rel, content_hash=_content_hash(text))
        self._index_body_links(note)
        if moving and old_rel:
            self._remove_note_file(old_rel)
        if changes is not None:
            changes.record_after(rel, path)
            if moving and old_rel:
                changes.record_after(old_rel, self.root / old_rel)
        return rel

    def link(
        self,
        src_target: str,
        dst_target: str,
        kind: str = "relates",
        *,
        changes: KnowledgeChanges | None = None,
    ) -> str:
        kind = kind if kind in LINK_KINDS else "relates"
        src_id = self.index.resolve(src_target)
        if src_id is None:
            return f"ERROR: unknown source note: {src_target}"
        src = self.read_note(src_id)
        if src is None:
            return f"ERROR: unknown source note: {src_target}"
        dst_id = self.index.resolve(dst_target)
        if dst_id is None:
            return f"ERROR: unknown target note: {dst_target}"
        dst = self.read_note(dst_id)
        display = dst.title or dst.id if dst else dst_id
        link_text = wikilink(display)
        if link_text in src.body:
            upgraded = _annotate_wikilink(src.body, link_text, kind)
            if upgraded is not None:
                src.body = upgraded
                self.write_note(src, changes=changes)
                self.index.add_link(src_id, dst_id, kind)
                return f"updated link: {src_id} -> {dst_id} ({kind})"
            self.index.add_link(src_id, dst_id, kind)
            return f"already linked: {src_id} -> {dst_id} ({kind})"
        src.body = _append_related(src.body, f"{link_text} ({kind})")
        self.write_note(src, changes=changes)
        self.index.add_link(src_id, dst_id, kind)
        return f"linked: {src_id} -> {dst_id} ({kind})"

    def read_note(self, note_id: str) -> KnowledgeNote | None:
        row = self.index.get(note_id)
        if row and row.get("path"):
            path = self.root / row["path"]
            if path.is_file():
                return KnowledgeNote.from_markdown(path.read_text(encoding="utf-8"))
        for path in self.iter_note_paths():
            if path.stem == note_id:
                return KnowledgeNote.from_markdown(path.read_text(encoding="utf-8"))
        return None

    def iter_note_paths(self):
        for path in sorted(self.root.rglob("*.md")):
            if ".codey" in path.parts:
                continue
            yield path

    def rebuild(self) -> int:
        self.index.clear()
        notes: list[KnowledgeNote] = []
        for path in self.iter_note_paths():
            try:
                text = path.read_text(encoding="utf-8")
                note = KnowledgeNote.from_markdown(text)
            except (ValueError, OSError):
                continue
            self.index.upsert(note, path=self.rel(path), content_hash=_content_hash(text))
            notes.append(note)
        for note in notes:
            self._index_body_links(note)
        return len(notes)

    def close(self) -> None:
        self.index.close()

    def __del__(self) -> None:
        try:
            self.close()
        except Exception:
            pass

    def _current_path(self, note_id: str) -> str | None:
        row = self.index.get(note_id)
        return (row.get("path") or None) if row else None

    def _remove_note_file(self, rel: str) -> None:
        stale = (self.root / rel).resolve()
        if stale != self.root and self.root not in stale.parents:
            return
        if stale.is_file():
            try:
                stale.unlink()
            except OSError:
                pass

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


def _atomic_write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        with temporary.open("w", encoding="utf-8", newline="\n") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        try:
            temporary.unlink()
        except OSError:
            pass
