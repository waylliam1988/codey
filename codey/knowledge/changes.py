"""Per-run snapshots and restore for knowledge notes."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from pathlib import Path

MAX_SNAPSHOT_FILE_BYTES = 512 * 1024
MAX_SNAPSHOT_FILES = 200
MAX_SNAPSHOT_TOTAL_BYTES = 32 * 1024 * 1024


@dataclass(frozen=True)
class RestoreResult:
    ok: bool
    restored: list[str]
    conflicts: list[str]
    error: str | None = None


@dataclass(frozen=True)
class KnowledgeChangesSnapshot:
    before: dict[str, str | None]
    after_hashes: dict[str, str]
    touched: frozenset[str]
    last_restore_result: RestoreResult | None = None


@dataclass
class KnowledgeChanges:
    root: Path
    _before: dict[str, str | None] = field(default_factory=dict)
    _after_hashes: dict[str, str] = field(default_factory=dict)
    _touched: set[str] = field(default_factory=set)
    last_restore_result: RestoreResult | None = None

    def capture_before(self, rel: str, path: Path) -> None:
        path = _safe_join(self.root, rel)
        rel = path.relative_to(Path(self.root).expanduser().resolve()).as_posix()
        if rel in self._before:
            return
        before = _read_text_or_none(path)
        self._validate_capacity(rel, before)
        self._before[rel] = before

    def record_after(self, rel: str, path: Path) -> None:
        path = _safe_join(self.root, rel)
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

    def summary(self) -> dict:
        return {"created": self.created, "updated": self.updated}

    def has_changes(self) -> bool:
        return bool(self._touched)

    def snapshot(self) -> KnowledgeChangesSnapshot:
        return KnowledgeChangesSnapshot(
            before=dict(self._before),
            after_hashes=dict(self._after_hashes),
            touched=frozenset(self._touched),
            last_restore_result=self.last_restore_result,
        )

    def restore_snapshot(self, snapshot: KnowledgeChangesSnapshot) -> None:
        self._before = dict(snapshot.before)
        self._after_hashes = dict(snapshot.after_hashes)
        self._touched = set(snapshot.touched)
        self.last_restore_result = snapshot.last_restore_result

    def restore_result(self) -> RestoreResult:
        restored: list[str] = []
        conflicts: list[str] = []
        root = Path(self.root).expanduser().resolve()
        for rel in sorted(self._touched):
            try:
                path = _safe_join(root, rel)
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
                    path.write_text(original, encoding="utf-8", newline="\n")
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
        self._after_hashes.pop(rel, None)
        self._touched.discard(rel)


def _safe_join(root: Path, rel: str) -> Path:
    resolved_root = Path(root).expanduser().resolve()
    path = (resolved_root / str(rel)).resolve()
    if path != resolved_root and resolved_root not in path.parents:
        raise ValueError(f"path escapes knowledge root: {rel}")
    return path


def _read_text_or_none(path: Path) -> str | None:
    if not path.exists():
        return None
    if not path.is_file():
        raise ValueError(f"not a file: {path}")
    if path.stat().st_size > MAX_SNAPSHOT_FILE_BYTES:
        raise ValueError(f"file too large for snapshot: {path}")
    return path.read_text(encoding="utf-8")


def _content_hash(content: str | None) -> str:
    if content is None:
        return "missing"
    return "sha256:" + hashlib.sha256(content.encode("utf-8")).hexdigest()


def _path_hash(path: Path) -> str:
    if not path.exists():
        return "missing"
    if not path.is_file():
        raise ValueError(f"not a file: {path}")
    digest = hashlib.sha256()
    with path.open("r", encoding="utf-8") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), ""):
            digest.update(chunk.encode("utf-8"))
    return "sha256:" + digest.hexdigest()
