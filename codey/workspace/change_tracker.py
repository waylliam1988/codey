"""Task change tracking, diff rendering, and conflict-aware snapshot restoration."""

from __future__ import annotations

import difflib
import threading
from dataclasses import dataclass
from html.parser import HTMLParser
from io import StringIO
from pathlib import Path
from typing import Literal

from codey.storage.atomic_io import write_text_atomic
from codey.storage.local_store import StoreCorruption
from codey.workspace.paths import content_hash as _content_hash
from codey.workspace.paths import ensure_not_symlink as _ensure_not_symlink
from codey.workspace.paths import path_hash as _path_hash
from codey.workspace.paths import read_text_or_none as _read_text_or_none
from codey.workspace.paths import safe_join as _safe_join
from codey.workspace.snapshot_store import (
    MAX_SNAPSHOT_FILE_BYTES,
    MAX_SNAPSHOT_FILES,
    MAX_SNAPSHOT_TOTAL_BYTES,
    SnapshotStore,
)

MAX_SNAPSHOT_DIFF_CHARS = 240_000


@dataclass(frozen=True)
class Snapshot:
    path: str
    before: str | None
    after: str | None


class _PreviewDependencies(HTMLParser):
    """Recognize resource references that the single-file sandbox cannot load."""

    def __init__(self) -> None:
        super().__init__()
        self.external = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if self.external:
            return
        for attr, value in attrs:
            loads_resource = attr in {"src", "poster"} or tag == "link" and attr == "href"
            if attr == "srcset" or (
                loads_resource and value and not value.strip().casefold().startswith(("data:", "#"))
            ):
                self.external = True
                return


def _standalone_preview(content: str) -> bool:
    parser = _PreviewDependencies()
    parser.feed(content)
    parser.close()
    return not parser.external


@dataclass(frozen=True)
class RestoreResult:
    ok: bool
    restored: list[str]
    conflicts: list[str]
    error: str | None = None


RestoreState = Literal["already_restored", "restorable", "conflict", "unavailable"]


def _restore_state(before: str, current: str, recorded_after: str | None) -> RestoreState:
    if before == current:
        return "already_restored"
    if recorded_after is not None and recorded_after == current:
        return "restorable"
    return "conflict"


def _diff_and_counts(
    path: str,
    before: str | None,
    after: str | None,
) -> tuple[str, int, int]:
    before_lines = StringIO(before or "").readlines()
    after_lines = StringIO(after or "").readlines()
    fromfile = "/dev/null" if before is None else f"a/{path}"
    tofile = "/dev/null" if after is None else f"b/{path}"
    diff_lines = list(
        difflib.unified_diff(
            before_lines,
            after_lines,
            fromfile=fromfile,
            tofile=tofile,
        )
    )
    additions = 0
    deletions = 0
    for line in diff_lines[2:]:
        if line.startswith("+"):
            additions += 1
        elif line.startswith("-"):
            deletions += 1
    body = "".join(line if line.endswith("\n") else line + "\n\\ No newline at end of file\n"
                   for line in diff_lines).removesuffix("\n")
    diff_text = f"diff --git a/{path} b/{path}\n{body}" if body else ""
    return diff_text, additions, deletions


def _status_for(before: str | None, after: str | None) -> str:
    if before is None and after is not None:
        return "A"
    if before is not None and after is None:
        return "D"
    return "M"


class ChangeTracker:
    """First-write baselines; locked collect, explicit prune_clean."""

    def __init__(
        self,
        root: str | Path,
        store: SnapshotStore | None = None,
    ) -> None:
        self.root = Path(root).expanduser().resolve()
        self.store = store
        self._lock = threading.RLock()
        if store is None:
            self._before: dict[str, str | None] = {}
            self._after_hashes: dict[str, str] = {}
        else:
            self._before, self._after_hashes = store.load(self.root)
        self._total_bytes = sum(
            len((value or "").encode("utf-8")) for value in self._before.values()
        )

    @property
    def has_snapshots(self) -> bool:
        with self._lock:
            return bool(self._before)

    def disable_persistence(self) -> None:
        with self._lock:
            self.store = None

    def _validate_capacity_locked(self, rel: str, content: str | None) -> None:
        if rel not in self._before and len(self._before) >= MAX_SNAPSHOT_FILES:
            raise ValueError("snapshot file limit reached")
        total = self._total_bytes
        if rel not in self._before:
            total += len((content or "").encode("utf-8"))
        if total > MAX_SNAPSHOT_TOTAL_BYTES:
            raise ValueError("snapshot size limit reached")

    def capture_before(self, rel: str) -> None:
        path = _safe_join(self.root, rel)
        rel_posix = path.relative_to(self.root).as_posix()
        with self._lock:
            cached = rel_posix in self._before
            cached_value = self._before.get(rel_posix) if cached else None
            store = self.store
        if cached:
            # Cached paths re-validate the durable basis read-only: a lost
            # manifest or entry, or a disk value that no longer matches
            # memory, is corruption -- never rebuilt from the working file.
            if store is not None:
                cached_persisted = store.require_baseline(self.root, rel_posix)
                if cached_persisted != cached_value:
                    raise StoreCorruption(
                        store.path_for(self.root),
                        f"baseline drift: {rel_posix!r}",
                    )
            return
        before = _read_text_or_none(path, max_bytes=MAX_SNAPSHOT_FILE_BYTES)
        with self._lock:
            # Pre-check memory capacity before touching disk: a rejected new
            # entry must never appear in the manifest.
            self._validate_capacity_locked(rel_posix, before)
        # Disk wins: the persisted baseline is the memory value, so a second
        # tracker reading a newer file converges back to the first writer
        # instead of forking. Damaged disk entries raise and publish nothing.
        persisted: str | None = before
        if store is not None:
            persisted = store.put_baseline(self.root, rel_posix, before)
        added = len((persisted or "").encode("utf-8"))
        with self._lock:
            if rel_posix in self._before:
                return
            self._validate_capacity_locked(rel_posix, persisted)
            self._before[rel_posix] = persisted
            self._total_bytes += added

    def capture_after(self, rel: str) -> None:
        path = _safe_join(self.root, rel)
        rel_posix = path.relative_to(self.root).as_posix()
        with self._lock:
            if rel_posix not in self._before:
                return
        digest = _path_hash(path)
        # Hashing also happens outside the lock; prune_clean may have dropped
        # the baseline meanwhile. Re-check membership so no orphan after-hash
        # outlives its baseline.
        with self._lock:
            if rel_posix not in self._before:
                return
            self._after_hashes[rel_posix] = digest
            store = self.store
        if store is not None:
            store.set_after_hash(self.root, rel_posix, digest)

    def snapshots(self, paths: list[str] | None = None) -> list[Snapshot]:
        # One locked copy of {path: baseline}: the whole collect sees a single
        # start view, and a concurrent prune_clean can no longer remove a key
        # between listing and lookup (KeyError). File reads stay outside the
        # lock; only this small dict is copied under it.
        with self._lock:
            view = dict(self._before)
            selected = set(paths) if paths is not None else set(view)
        items: list[Snapshot] = []
        for rel in sorted(view):
            if rel not in selected:
                continue
            before = view[rel]
            path = _safe_join(self.root, rel)
            after = _read_text_or_none(path, max_bytes=MAX_SNAPSHOT_FILE_BYTES)
            if before == after:
                continue
            items.append(Snapshot(rel, before, after))
        return items

    def collect(self, *, prune_clean: bool = False, paths: list[str] | None = None) -> dict[str, object]:
        """Render the current change set; read-only unless pruning."""

        snapshots = self.snapshots(paths)
        if prune_clean:
            self.prune_clean(skip={snapshot.path for snapshot in snapshots})
        return self._render_snapshots(snapshots)

    def collect_produced(self) -> dict[str, object]:
        """Compare and render the same bounded read of each task output."""
        with self._lock:
            outputs = [(path, self._before[path], digest) for path, digest in sorted(self._after_hashes.items())]
        snapshots: list[Snapshot] = []
        for rel, before, expected in outputs:
            try:
                after = _read_text_or_none(_safe_join(self.root, rel), max_bytes=MAX_SNAPSHOT_FILE_BYTES)
            except (OSError, ValueError):
                return self._unavailable_task_changes(f"Task output could not be read: {rel}")
            if _content_hash(after) != expected:
                return self._unavailable_task_changes(f"Task output changed externally: {rel}")
            if before != after:
                snapshots.append(Snapshot(rel, before, after))
        changes = self._render_snapshots(snapshots)
        previews: dict[str, str] = {}
        remaining = 128 * 1024
        for snapshot in snapshots:
            if snapshot.after is None or Path(snapshot.path).suffix.casefold() not in {".html", ".htm"}:
                continue
            size = len(snapshot.after.encode("utf-8"))
            if size and size <= remaining and _standalone_preview(snapshot.after):
                previews[snapshot.path] = snapshot.after
                remaining -= size
        if previews:
            changes["html_previews"] = previews
        return changes

    def _unavailable_task_changes(self, error: str) -> dict[str, object]:
        return {**self._render_snapshots([]), "ok": False, "error": error}

    def _render_snapshots(self, snapshots: list[Snapshot]) -> dict[str, object]:
        files = []
        diff_parts = []
        for snapshot in snapshots:
            diff, additions, deletions = _diff_and_counts(
                snapshot.path,
                snapshot.before,
                snapshot.after,
            )
            files.append(
                {
                    "path": snapshot.path,
                    "status": _status_for(snapshot.before, snapshot.after),
                    "additions": additions,
                    "deletions": deletions,
                }
            )
            if diff:
                diff_parts.append(diff)

        diff_text = "\n\n".join(diff_parts)
        truncated = len(diff_text) > MAX_SNAPSHOT_DIFF_CHARS
        if truncated:
            diff_text = diff_text[:MAX_SNAPSHOT_DIFF_CHARS].rstrip() + "\n\n... diff truncated ..."
        return {
            "ok": True,
            "mode": "snapshot",
            "root": str(self.root),
            "files": files,
            "changed_count": len(files),
            "diff": diff_text,
            "truncated": truncated,
        }

    def prune_clean(self, *, skip: set[str] | None = None) -> list[str]:
        """Drop baselines whose file matches its original content.

        Called at run terminal states, never from read-only collection, so
        concurrent UI polling cannot erase recovery state mid-run.
        """

        ignored = skip or set()
        pruned: list[str] = []
        with self._lock:
            tracked = list(self._before)
        for rel in tracked:
            if rel in ignored:
                continue
            try:
                current = _read_text_or_none(_safe_join(self.root, rel), max_bytes=MAX_SNAPSHOT_FILE_BYTES)
            except (OSError, UnicodeDecodeError, ValueError):
                continue
            with self._lock:
                unchanged = rel in self._before and current == self._before[rel]
                if unchanged:
                    self._forget_locked(rel)
            if unchanged:
                pruned.append(rel)
        return pruned

    def _forget_locked(self, rel: str) -> None:
        # Persistent removal first: a disk failure keeps the in-memory
        # tracking so the next run still sees the old baseline instead of
        # silently losing its recovery point.
        with self._lock:
            store = self.store
        if store is not None:
            store.remove(self.root, rel)
        with self._lock:
            content = self._before.pop(rel, None)
            self._after_hashes.pop(rel, None)
            self._total_bytes -= len((content or "").encode("utf-8"))

    def _restore_paths(self, paths: list[str] | None) -> list[str]:
        with self._lock:
            return sorted(set(self._before if paths is None else paths))

    def _restore_status(self, rel: str) -> tuple[RestoreState, Path | None]:
        with self._lock:
            if rel not in self._before:
                return "unavailable", None
            baseline = _content_hash(self._before[rel])
            expected_hash = self._after_hashes.get(rel)
        try:
            path = _safe_join(self.root, rel)
            if path.relative_to(self.root).as_posix() != rel:
                return "unavailable", None
            _ensure_not_symlink(path)
            return _restore_state(baseline, _path_hash(path), expected_hash), path
        except (OSError, ValueError):
            return "unavailable", None

    def preview_restore(self, paths: list[str] | None = None) -> tuple[tuple[str, RestoreState], ...]:
        """Observe current per-file recoverability without removing any baselines."""
        return tuple((rel, self._restore_status(rel)[0]) for rel in self._restore_paths(paths))

    def restore(self, paths: list[str] | None = None, *, keep_baselines: bool = False) -> RestoreResult:
        restored: list[str] = []
        conflicts: list[str] = []

        for rel in self._restore_paths(paths):
            with self._lock:
                state, path = self._restore_status(rel)
                if state == "already_restored":
                    if not keep_baselines:
                        self._forget_locked(rel)
                    continue
                if state != "restorable" or path is None:
                    conflicts.append(rel)
                    continue
                before = self._before[rel]
                try:
                    _ensure_not_symlink(path)
                    if before is None:
                        path.unlink(missing_ok=True)
                    else:
                        write_text_atomic(path, before)
                except (OSError, ValueError):
                    conflicts.append(rel)
                    continue
                restored.append(rel)
                if not keep_baselines:
                    self._forget_locked(rel)

        return RestoreResult(not conflicts, restored, conflicts, None if not conflicts else "restore conflict")
