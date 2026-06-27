"""Task-level file change snapshots for non-Git projects."""

from __future__ import annotations

import difflib
from dataclasses import dataclass
from pathlib import Path


MAX_SNAPSHOT_FILE_BYTES = 512 * 1024
MAX_SNAPSHOT_DIFF_CHARS = 240_000


@dataclass(frozen=True)
class Snapshot:
    path: str
    before: str | None
    after: str | None


@dataclass(frozen=True)
class RestoreResult:
    ok: bool
    restored: list[str]
    conflicts: list[str]
    error: str | None = None


def _safe_join(root: Path, rel: str) -> Path:
    root_resolved = root.resolve()
    path = (root_resolved / rel).resolve()
    if root_resolved not in path.parents and path != root_resolved:
        raise ValueError(f"path escapes project root: {rel}")
    return path


def _read_text_or_none(path: Path) -> str | None:
    if not path.exists():
        return None
    if not path.is_file():
        raise ValueError(f"not a file: {path}")
    if path.stat().st_size > MAX_SNAPSHOT_FILE_BYTES:
        raise ValueError(f"file too large for snapshot: {path}")
    return path.read_text(encoding="utf-8")


def _change_counts(before: str | None, after: str | None) -> tuple[int, int]:
    before_lines = [] if before is None else before.splitlines()
    after_lines = [] if after is None else after.splitlines()
    additions = 0
    deletions = 0
    for tag, i1, i2, j1, j2 in difflib.SequenceMatcher(a=before_lines, b=after_lines).get_opcodes():
        if tag == "insert":
            additions += j2 - j1
        elif tag == "delete":
            deletions += i2 - i1
        elif tag == "replace":
            deletions += i2 - i1
            additions += j2 - j1
    return additions, deletions


def _status_for(before: str | None, after: str | None) -> str:
    if before is None and after is not None:
        return "A"
    if before is not None and after is None:
        return "D"
    return "M"


def _diff_for(path: str, before: str | None, after: str | None) -> str:
    before_lines = [] if before is None else before.splitlines(keepends=True)
    after_lines = [] if after is None else after.splitlines(keepends=True)
    fromfile = "/dev/null" if before is None else f"a/{path}"
    tofile = "/dev/null" if after is None else f"b/{path}"
    diff = difflib.unified_diff(before_lines, after_lines, fromfile=fromfile, tofile=tofile, lineterm="")
    body = "\n".join(diff)
    return f"diff --git a/{path} b/{path}\n{body}" if body else ""


class ChangeTracker:
    """Record first-write baselines and render diffs against current files."""

    def __init__(self, root: str | Path) -> None:
        self.root = Path(root).expanduser().resolve()
        self._before: dict[str, str | None] = {}
        self._after: dict[str, str | None] = {}

    def capture_before(self, rel: str) -> None:
        path = _safe_join(self.root, rel)
        rel_posix = path.relative_to(self.root).as_posix()
        if rel_posix in self._before:
            return
        self._before[rel_posix] = _read_text_or_none(path)

    def snapshots(self, paths: list[str] | None = None) -> list[Snapshot]:
        selected = set(paths or self._before.keys())
        items: list[Snapshot] = []
        for rel in sorted(self._before):
            if rel not in selected:
                continue
            path = _safe_join(self.root, rel)
            try:
                after = _read_text_or_none(path)
            except (OSError, UnicodeDecodeError, ValueError):
                after = None
            before = self._before[rel]
            if before == after:
                continue
            items.append(Snapshot(rel, before, after))
        return items

    def collect(self) -> dict:
        snapshots = self.snapshots()
        files = []
        diff_parts = []
        for snapshot in snapshots:
            additions, deletions = _change_counts(snapshot.before, snapshot.after)
            self._after[snapshot.path] = snapshot.after
            files.append({
                "path": snapshot.path,
                "status": _status_for(snapshot.before, snapshot.after),
                "additions": additions,
                "deletions": deletions,
            })
            diff = _diff_for(snapshot.path, snapshot.before, snapshot.after)
            if diff:
                diff_parts.append(diff)

        diff_text = "\n\n".join(diff_parts)
        truncated = len(diff_text) > MAX_SNAPSHOT_DIFF_CHARS
        if truncated:
            diff_text = diff_text[:MAX_SNAPSHOT_DIFF_CHARS].rstrip() + "\n\n... diff truncated by Codey"
        return {
            "ok": True,
            "mode": "snapshot",
            "root": str(self.root),
            "files": files,
            "changed_count": len(files),
            "diff": diff_text,
            "truncated": truncated,
        }

    def restore(self, paths: list[str] | None = None) -> RestoreResult:
        selected = sorted(set(paths or self._before.keys()))
        restored: list[str] = []
        conflicts: list[str] = []

        for rel in selected:
            if rel not in self._before:
                conflicts.append(rel)
                continue
            path = _safe_join(self.root, rel)
            before = self._before[rel]
            if rel in self._after:
                expected_after = self._after[rel]
            else:
                try:
                    expected_after = _read_text_or_none(path)
                except (OSError, UnicodeDecodeError, ValueError):
                    conflicts.append(rel)
                    continue
            if before == expected_after:
                self._before.pop(rel, None)
                self._after.pop(rel, None)
                continue
            try:
                current = _read_text_or_none(path)
            except (OSError, UnicodeDecodeError, ValueError):
                conflicts.append(rel)
                continue
            if current != expected_after:
                conflicts.append(rel)
                continue
            if before is None:
                try:
                    if path.exists():
                        path.unlink()
                except OSError:
                    conflicts.append(rel)
                    continue
            else:
                try:
                    path.parent.mkdir(parents=True, exist_ok=True)
                    path.write_text(before, encoding="utf-8")
                except OSError:
                    conflicts.append(rel)
                    continue
            restored.append(rel)
            self._before.pop(rel, None)
            self._after.pop(rel, None)

        return RestoreResult(not conflicts, restored, conflicts, None if not conflicts else "restore conflict")
