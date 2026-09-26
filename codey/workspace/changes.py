"""Git and snapshot-backed project change collection and restoration."""

from __future__ import annotations

import contextlib
import difflib
import hashlib
import shutil
import subprocess
import threading
from dataclasses import dataclass
from pathlib import Path

from codey.storage.atomic_io import write_bytes_atomic, write_text_atomic
from codey.storage.file_lock import FileLease, LockTimeout, acquire_lease, with_file_lock
from codey.storage.local_store import (
    DEFAULT_STATE_HOME,
    StoreCorruption,
    delete_file,
    project_key,
    read_json_strict,
    write_json_atomic,
)
from codey.utils.change_paths import change_file_paths
from codey.workspace.paths import (
    content_hash as _content_hash,
)
from codey.workspace.paths import (
    ensure_not_symlink as _ensure_not_symlink,
)
from codey.workspace.paths import (
    path_hash as _path_hash,
)
from codey.workspace.paths import (
    read_text_bounded as _read_text_bounded,
)
from codey.workspace.paths import (
    read_text_or_none as _read_text_or_none,
)
from codey.workspace.paths import (
    safe_join as _safe_join,
)

MAX_SNAPSHOT_FILE_BYTES = 512 * 1024
MAX_SNAPSHOT_DIFF_CHARS = 240_000
MAX_SNAPSHOT_FILES = 200
MAX_SNAPSHOT_TOTAL_BYTES = 32 * 1024 * 1024
MAX_SNAPSHOT_MANIFEST_BYTES = 4 * 1024 * 1024
SNAPSHOT_SCHEMA_VERSION = 1
SNAPSHOT_DIR_NAME = "recovery"
BASELINE_DIR_NAME = "baselines"
GIT_TIMEOUT = 10
MAX_GIT_DIFF_CHARS = 240_000
MAX_GIT_OUTPUT_BYTES = 2 * 1024 * 1024
MAX_UNTRACKED_DIFF_BYTES = 120_000
# Internal collection reasons: only a confirmed non-repo or a missing git
# binary may fall back to a snapshot view. Command failures (timeouts,
# read errors, non-zero exits, truncation) carry git_failed and propagate.
GIT_REASON_NOT_REPO = "not_repo"
GIT_REASON_MISSING = "git_missing"
GIT_REASON_FAILED = "git_failed"
_GIT_FALLBACK_REASONS = frozenset({GIT_REASON_NOT_REPO, GIT_REASON_MISSING})
_GIT_STDERR_EXCERPT_CHARS = 500
CHANGE_EXCLUDED_PATH_PARTS = {
    "__pycache__",
    ".pytest_cache",
    ".mypy_cache",
    ".ruff_cache",
    "node_modules",
    ".next",
    "dist",
    "build",
}


class ProjectWriteBusy(RuntimeError):
    """A second task tried to persist snapshots for a project already owned."""


def _manifest_files_or_raise(payload: dict, manifest_path: Path) -> dict:
    """Return the top-level ``files`` index or raise ``StoreCorruption``.

    Per-entry dirt is skipped by callers; a missing/broken top-level index
    blocks writes so one edit never rebuilds an empty index over good bodies.
    The corrupt file stays in place for explicit repair.
    """
    if payload.get("schema_version") != SNAPSHOT_SCHEMA_VERSION:
        raise StoreCorruption(manifest_path, "manifest schema_version")
    raw_files = payload.get("files")
    if not isinstance(raw_files, dict):
        raise StoreCorruption(manifest_path, "manifest files not a dict")
    return raw_files


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


class SnapshotStore:
    """Persist one bounded recovery baseline per non-Git project.

    The store is two-layered so one edit no longer rewrites the whole
    baseline set:

    - ``recovery/baselines/<rel-digest>.txt`` holds one file's baseline body;
    - ``recovery/manifest.json`` is the small index (path -> baseline ref,
      after hash).

    ``capture_after`` therefore touches only the manifest, and a new
    baseline writes one bounded body file plus the manifest instead of
    re-serializing up to 64MB of JSON on every edit.
    """

    def __init__(self, state_home: str | Path = DEFAULT_STATE_HOME) -> None:
        self.state_home = Path(state_home)

    def dir_for(self, root: str | Path) -> Path:
        return (
            self.state_home
            / "projects"
            / project_key(root)
            / SNAPSHOT_DIR_NAME
        )

    def path_for(self, root: str | Path) -> Path:
        return self.dir_for(root) / "manifest.json"

    def _lock_target(self, root: str | Path) -> Path:
        resolved_root = Path(root).expanduser().resolve()
        return self.dir_for(resolved_root)

    def _baseline_path(self, root: str | Path, rel: str) -> Path:
        digest = hashlib.sha256(rel.encode("utf-8")).hexdigest()[:32]
        return self.dir_for(root) / BASELINE_DIR_NAME / f"{digest}.txt"

    def load(self, root: str | Path) -> tuple[dict[str, str | None], dict[str, str]]:
        resolved_root = Path(root).expanduser().resolve()
        manifest_path = self.path_for(resolved_root)
        try:
            if not manifest_path.exists():
                return {}, {}
        except OSError:
            return {}, {}
        with with_file_lock(self._lock_target(resolved_root)):
            # Missing -> empty. Present-but-unreadable or top-level-invalid
            # -> raise and keep the file in place for explicit repair.
            payload = read_json_strict(
                manifest_path,
                max_bytes=MAX_SNAPSHOT_MANIFEST_BYTES,
            )
            if payload is None:
                return {}, {}
            raw_files = _manifest_files_or_raise(payload, manifest_path)

            before: dict[str, str | None] = {}
            hashes: dict[str, str] = {}
            total = 0
            for rel, entry in raw_files.items():
                # One dirty entry must not wipe the whole baseline: skip it.
                if len(before) >= MAX_SNAPSHOT_FILES or not isinstance(rel, str):
                    continue
                if not isinstance(entry, dict):
                    continue
                if set(entry) - {"baseline", "after_hash"}:
                    continue
                if "baseline" not in entry:
                    continue
                try:
                    path = _safe_join(resolved_root, rel)
                    canonical = path.relative_to(resolved_root).as_posix()
                except (ValueError, OSError):
                    continue
                if canonical != rel:
                    continue
                content: str | None
                if entry.get("baseline") is None:
                    content = None
                else:
                    expected_body = self._baseline_path(resolved_root, rel).name
                    if entry.get("baseline") != expected_body:
                        continue
                    try:
                        body = _read_text_bounded(
                            self._baseline_path(resolved_root, rel),
                            max_bytes=MAX_SNAPSHOT_FILE_BYTES,
                        )
                    except (OSError, UnicodeDecodeError, ValueError):
                        continue
                    if len(body.encode("utf-8")) > MAX_SNAPSHOT_FILE_BYTES:
                        continue
                    content = body
                digest = entry.get("after_hash")
                if digest is not None and not (
                    isinstance(digest, str)
                    and (digest == "missing" or digest.startswith("sha256:"))
                ):
                    continue
                content_bytes = len((content or "").encode("utf-8"))
                if total + content_bytes > MAX_SNAPSHOT_TOTAL_BYTES:
                    break
                total += content_bytes
                before[rel] = content
                if isinstance(digest, str):
                    hashes[rel] = digest
            return before, hashes

    def writer_lock_path(self, root: str | Path) -> Path:
        """Per-project persistent-writer ownership lock (held for a task)."""
        return self.dir_for(root) / ".writer.lock"

    def acquire_writer(self, root: str | Path, *, timeout_seconds: float = 0.0) -> FileLease:
        """Claim the single persistent writer for ``root`` or raise busy.

        Non-blocking by default so a second write task fails fast with
        ``ProjectWriteBusy`` instead of queueing behind the first.
        """
        self.dir_for(root).mkdir(parents=True, exist_ok=True)
        try:
            return acquire_lease(
                self.writer_lock_path(root), timeout_seconds=timeout_seconds
            )
        except LockTimeout as exc:
            raise ProjectWriteBusy(
                f"another task is writing this project: {root}"
            ) from exc

    def put_baseline(
        self,
        root: str | Path,
        rel: str,
        content: str | None,
    ) -> str | None:
        """First-write baseline; returns the persisted baseline.

        The on-disk entry always wins: a second tracker adopting a newer
        file read converges back to the first persisted value instead of
        forking memory. A damaged existing entry (bad schema, wrong body
        name, missing/unreadable body) raises and blocks further edits
        rather than forking or silently overwriting.
        """

        resolved_root = Path(root).expanduser().resolve()
        try:
            probe = _safe_join(resolved_root, rel)
            canonical = probe.relative_to(resolved_root).as_posix()
        except (ValueError, OSError) as exc:
            raise ValueError(f"unsafe snapshot path: {rel!r}") from exc
        if canonical != rel:
            raise ValueError(f"unsafe snapshot path: {rel!r}")
        if content is not None and len(content.encode("utf-8")) > MAX_SNAPSHOT_FILE_BYTES:
            raise ValueError(f"snapshot too large: {rel!r}")
        body_path = self._baseline_path(resolved_root, rel)
        manifest_path = self.path_for(resolved_root)

        with with_file_lock(self._lock_target(resolved_root)):
            payload = read_json_strict(manifest_path, max_bytes=MAX_SNAPSHOT_MANIFEST_BYTES)
            if payload is None:
                files: dict = {}
            else:
                files = _manifest_files_or_raise(payload, manifest_path)
            if rel in files:
                persisted = self._persisted_baseline_locked(
                    resolved_root, rel, files[rel], body_path
                )
                return persisted
            if len(files) >= MAX_SNAPSHOT_FILES:
                raise ValueError("snapshot file limit reached")
            if content is not None:
                new_bytes = len(content.encode("utf-8"))
                if self._disk_total_bytes_locked(resolved_root, files) + new_bytes > MAX_SNAPSHOT_TOTAL_BYTES:
                    raise ValueError("snapshot size limit reached")
            written_body = False
            try:
                if content is None:
                    _remove_file(body_path)
                else:
                    _write_bytes_atomic(body_path, content.encode("utf-8"))
                    written_body = True
                files[rel] = {
                    "baseline": None if content is None else body_path.name,
                }
                write_json_atomic(
                    manifest_path,
                    {"schema_version": SNAPSHOT_SCHEMA_VERSION, "files": files},
                    max_bytes=MAX_SNAPSHOT_MANIFEST_BYTES,
                )
                return content
            except Exception:
                if written_body:
                    _remove_file(body_path)
                raise

    def require_baseline(self, root: str | Path, rel: str) -> str | None:
        """Read-only recovery basis; never writes.

        The manifest and the entry must both exist. Missing or damaged
        state raises ``StoreCorruption`` so a lost manifest can never be
        rebuilt from the current working file.
        """
        resolved_root = Path(root).expanduser().resolve()
        try:
            probe = _safe_join(resolved_root, rel)
            canonical = probe.relative_to(resolved_root).as_posix()
        except (ValueError, OSError) as exc:
            raise ValueError(f"unsafe snapshot path: {rel!r}") from exc
        if canonical != rel:
            raise ValueError(f"unsafe snapshot path: {rel!r}")
        body_path = self._baseline_path(resolved_root, rel)
        manifest_path = self.path_for(resolved_root)
        with with_file_lock(self._lock_target(resolved_root)):
            payload = read_json_strict(manifest_path, max_bytes=MAX_SNAPSHOT_MANIFEST_BYTES)
            if payload is None:
                raise StoreCorruption(manifest_path, f"baseline manifest missing: {rel!r}")
            files = _manifest_files_or_raise(payload, manifest_path)
            if rel not in files:
                raise StoreCorruption(manifest_path, f"baseline entry missing: {rel!r}")
            return self._persisted_baseline_locked(resolved_root, rel, files[rel], body_path)

    def _persisted_baseline_locked(
        self,
        resolved_root: Path,
        rel: str,
        existing: object,
        body_path: Path,
    ) -> str | None:
        """Validate an existing entry inside the manifest lock and read it."""
        if not isinstance(existing, dict):
            raise StoreCorruption(
                self.path_for(resolved_root), f"baseline entry not an object: {rel!r}"
            )
        if set(existing) - {"baseline", "after_hash"} or "baseline" not in existing:
            raise StoreCorruption(
                self.path_for(resolved_root), f"baseline entry schema: {rel!r}"
            )
        baseline_ref = existing.get("baseline")
        if baseline_ref is None:
            digest = existing.get("after_hash")
            if digest is not None and not (
                isinstance(digest, str)
                and (digest == "missing" or digest.startswith("sha256:"))
            ):
                raise StoreCorruption(
                    self.path_for(resolved_root), f"baseline after_hash: {rel!r}"
                )
            return None
        if not isinstance(baseline_ref, str) or baseline_ref != body_path.name:
            raise StoreCorruption(
                self.path_for(resolved_root), f"baseline body name: {rel!r}"
            )
        digest = existing.get("after_hash")
        if digest is not None and not (
            isinstance(digest, str)
            and (digest == "missing" or digest.startswith("sha256:"))
        ):
            raise StoreCorruption(
                self.path_for(resolved_root), f"baseline after_hash: {rel!r}"
            )
        try:
            body = _read_text_bounded(body_path, max_bytes=MAX_SNAPSHOT_FILE_BYTES)
        except (OSError, UnicodeDecodeError, ValueError) as exc:
            raise StoreCorruption(
                self.path_for(resolved_root), f"baseline body unreadable: {rel!r}"
            ) from exc
        if len(body.encode("utf-8")) > MAX_SNAPSHOT_FILE_BYTES:
            raise StoreCorruption(
                self.path_for(resolved_root), f"baseline body too large: {rel!r}"
            )
        return body

    def _disk_total_bytes_locked(self, resolved_root: Path, files: dict) -> int:
        # Disk-view size guard from manifest refs + stat sizes only.
        # Missing, renamed, linked, or non-regular bodies are corruption:
        # never silently skipped, never re-decoded here.
        import stat as _stat

        total = 0
        for rel, entry in files.items():
            if not isinstance(entry, dict) or entry.get("baseline") is None:
                continue
            if not isinstance(rel, str):
                raise StoreCorruption(self.path_for(resolved_root), "baseline entry path")
            baseline_ref = entry.get("baseline")
            if not isinstance(baseline_ref, str):
                raise StoreCorruption(self.path_for(resolved_root), f"baseline body name: {rel!r}")
            if baseline_ref != self._baseline_path(resolved_root, rel).name:
                raise StoreCorruption(self.path_for(resolved_root), f"baseline body name: {rel!r}")
            body_path = self._baseline_path(resolved_root, rel)
            try:
                if body_path.is_symlink():
                    raise StoreCorruption(self.path_for(resolved_root), f"baseline body link: {rel!r}")
                file_stat = body_path.stat()
            except OSError as exc:
                raise StoreCorruption(
                    self.path_for(resolved_root), f"baseline body missing: {rel!r}"
                ) from exc
            if not _stat.S_ISREG(file_stat.st_mode):
                raise StoreCorruption(self.path_for(resolved_root), f"baseline body type: {rel!r}")
            total += int(file_stat.st_size)
            if total > MAX_SNAPSHOT_TOTAL_BYTES:
                break
        return total

    def set_after_hash(self, root: str | Path, rel: str, digest: str) -> None:
        resolved_root = Path(root).expanduser().resolve()
        with with_file_lock(self._lock_target(resolved_root)):
            self._update_manifest_locked(
                resolved_root,
                rel,
                lambda entry: {**entry, "after_hash": digest},
            )

    def remove(self, root: str | Path, rel: str) -> None:
        """Drop one entry; manifest first, body last (failures preserve)."""

        resolved_root = Path(root).expanduser().resolve()
        snapshot_dir = self.dir_for(resolved_root)
        try:
            if not snapshot_dir.exists():
                return
        except OSError:
            return
        body_path = self._baseline_path(resolved_root, rel)
        manifest_path = self.path_for(resolved_root)

        with with_file_lock(self._lock_target(resolved_root)):
            payload = read_json_strict(manifest_path, max_bytes=MAX_SNAPSHOT_MANIFEST_BYTES)
            if payload is None:
                _remove_file(body_path)
                _remove_dir_if_empty(body_path.parent)
                return
            files = _manifest_files_or_raise(payload, manifest_path)
            if rel not in files:
                _remove_file(body_path)
                _remove_dir_if_empty(body_path.parent)
                return
            del files[rel]
            if files:
                write_json_atomic(
                    manifest_path,
                    {"schema_version": SNAPSHOT_SCHEMA_VERSION, "files": files},
                    max_bytes=MAX_SNAPSHOT_MANIFEST_BYTES,
                )
            else:
                delete_file(manifest_path)
                _remove_dir_if_empty(self.dir_for(resolved_root))
            _remove_file(body_path)
            _remove_dir_if_empty(body_path.parent)

    def delete(self, root: str | Path) -> None:
        resolved_root = Path(root).expanduser().resolve()
        snapshot_dir = self.dir_for(resolved_root)
        try:
            if not snapshot_dir.exists():
                return
        except OSError:
            return
        with with_file_lock(self._lock_target(resolved_root)):
            try:
                shutil.rmtree(snapshot_dir)
            except FileNotFoundError:
                return
            except OSError:
                return

    def _update_manifest_locked(
        self,
        resolved_root: Path,
        rel: str,
        mutate,
    ) -> None:
        manifest_path = self.path_for(resolved_root)
        payload = read_json_strict(manifest_path, max_bytes=MAX_SNAPSHOT_MANIFEST_BYTES)
        if payload is None:
            raise StoreCorruption(manifest_path, "missing manifest for update")
        files = _manifest_files_or_raise(payload, manifest_path)
        if rel not in files:
            raise StoreCorruption(manifest_path, f"missing baseline entry: {rel!r}")
        entry = files.get(rel)
        if not isinstance(entry, dict) or "baseline" not in entry:
            raise StoreCorruption(manifest_path, f"baseline entry schema: {rel!r}")
        updated = mutate(dict(entry))
        if not isinstance(updated, dict) or "baseline" not in updated:
            raise StoreCorruption(manifest_path, f"baseline entry schema: {rel!r}")
        files[rel] = updated
        write_json_atomic(
            manifest_path,
            {"schema_version": SNAPSHOT_SCHEMA_VERSION, "files": files},
            max_bytes=MAX_SNAPSHOT_MANIFEST_BYTES,
        )


def _write_bytes_atomic(path: Path, data: bytes) -> None:
    write_bytes_atomic(path, data)


def _remove_file(path: Path) -> None:
    with contextlib.suppress(OSError):
        path.unlink(missing_ok=True)


def _remove_dir_if_empty(directory: Path) -> None:
    with contextlib.suppress(OSError):
        directory.rmdir()


def _diff_and_counts(
    path: str,
    before: str | None,
    after: str | None,
) -> tuple[str, int, int]:
    # splitlines() without keepends + lineterm="": keeping line endings here
    # made every content line double-spaced in the rendered diff.
    before_lines = [] if before is None else before.splitlines()
    after_lines = [] if after is None else after.splitlines()
    fromfile = "/dev/null" if before is None else f"a/{path}"
    tofile = "/dev/null" if after is None else f"b/{path}"
    diff_lines = list(
        difflib.unified_diff(
            before_lines,
            after_lines,
            fromfile=fromfile,
            tofile=tofile,
            lineterm="",
        )
    )
    additions = 0
    deletions = 0
    for line in diff_lines:
        if line.startswith("+") and not line.startswith("+++"):
            additions += 1
        elif line.startswith("-") and not line.startswith("---"):
            deletions += 1
    body = "\n".join(diff_lines)
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
                persisted = store.require_baseline(self.root, rel_posix)
                if persisted != cached_value:
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
        try:
            digest = _path_hash(path)
        except (OSError, UnicodeDecodeError, ValueError):
            return
        # Hashing also happens outside the lock; prune_clean may have dropped
        # the baseline meanwhile. Re-check membership so no orphan after-hash
        # outlives its baseline.
        with self._lock:
            if rel_posix not in self._before:
                return
            self._after_hashes[rel_posix] = digest
            store = self.store
        if store is not None:
            with contextlib.suppress(OSError, ValueError):
                store.set_after_hash(self.root, rel_posix, digest)

    def snapshots(self, paths: list[str] | None = None) -> list[Snapshot]:
        # One locked copy of {path: baseline}: the whole collect sees a single
        # start view, and a concurrent prune_clean can no longer remove a key
        # between listing and lookup (KeyError). File reads stay outside the
        # lock; only this small dict is copied under it.
        with self._lock:
            view = dict(self._before)
            selected = set(paths) if paths else set(view)
        items: list[Snapshot] = []
        for rel in sorted(view):
            if rel not in selected:
                continue
            before = view[rel]
            path = _safe_join(self.root, rel)
            try:
                after = _read_text_or_none(path, max_bytes=MAX_SNAPSHOT_FILE_BYTES)
            except (OSError, UnicodeDecodeError, ValueError):
                continue
            if before == after:
                continue
            items.append(Snapshot(rel, before, after))
        return items

    def collect(self, *, prune_clean: bool = False) -> dict:
        """Render the current change set; read-only unless pruning."""

        snapshots = self.snapshots()
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
        changed_paths = {snapshot.path for snapshot in snapshots}
        if prune_clean:
            self.prune_clean(skip=changed_paths)
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

    def restore(self, paths: list[str] | None = None) -> RestoreResult:
        with self._lock:
            selected = sorted(set(paths or self._before.keys()))
        restored: list[str] = []
        conflicts: list[str] = []

        for rel in selected:
            with self._lock:
                if rel not in self._before:
                    conflicts.append(rel)
                    continue
                before = self._before[rel]
                expected_hash = self._after_hashes.get(rel)
            path = _safe_join(self.root, rel)
            try:
                _ensure_not_symlink(path)
                current_hash = _path_hash(path)
            except (OSError, ValueError):
                conflicts.append(rel)
                continue
            if _content_hash(before) == current_hash:
                with self._lock:
                    self._forget_locked(rel)
                continue
            if expected_hash is None or current_hash != expected_hash:
                conflicts.append(rel)
                continue
            try:
                if path.is_symlink():
                    # Fail closed: never write restored content through a
                    # swapped-in link, even though os.replace would swap the
                    # link itself. Deleting a link to restore absence is safe.
                    if before is not None:
                        conflicts.append(rel)
                        continue
                elif before is not None:
                    _ensure_not_symlink(path)
            except (OSError, ValueError):
                conflicts.append(rel)
                continue
            if before is None:
                try:
                    if path.is_symlink() or path.exists():
                        path.unlink()
                except OSError:
                    conflicts.append(rel)
                    continue
            else:
                try:
                    write_text_atomic(path, before)
                except OSError:
                    conflicts.append(rel)
                    continue
            restored.append(rel)
            with self._lock:
                self._forget_locked(rel)

        return RestoreResult(not conflicts, restored, conflicts, None if not conflicts else "restore conflict")


def _run_git(project: Path, args: list[str]):
    """Run git with bounded capture; never buffers output unbounded."""
    from codey.runtime.core import cancellation

    return cancellation.run_process(
        ["git", "-c", "core.quotePath=false", "-C", str(project), *args],
        cwd=str(project),
        timeout=GIT_TIMEOUT,
        capture_limit_bytes=MAX_GIT_OUTPUT_BYTES,
    )


def is_git_repository(project: str | Path) -> bool:
    from codey.runtime.core import cancellation as _cancellation

    try:
        proc = _run_git(
            Path(project).expanduser().resolve(),
            ["rev-parse", "--is-inside-work-tree"],
        )
    except (
        FileNotFoundError,
        OSError,
        subprocess.SubprocessError,
        _cancellation.ProcessOutputReadError,
        _cancellation.PipeDrainTimeout,
    ):
        return False
    if proc.stdout_truncated:
        return False
    return proc.returncode == 0 and proc.stdout.strip() == "true"


def parse_git_status(short_status: str) -> list[dict]:
    files: list[dict] = []
    for line in short_status.splitlines():
        if not line.strip():
            continue
        status = line[:2].strip() or "M"
        raw_path = line[3:].strip() if len(line) > 3 else line[2:].strip()
        path, previous_path = change_file_paths(raw_path, "", status)
        if not path:
            continue
        item = {"path": path, "status": status, "additions": 0, "deletions": 0}
        if previous_path:
            item["previous_path"] = previous_path
        files.append(item)
    return files


def is_displayable_change_path(path: str) -> bool:
    normalized = (path or "").replace("\\", "/").strip("/")
    parts = [part for part in normalized.split("/") if part and part != "->"]
    return not any(part in CHANGE_EXCLUDED_PATH_PARTS for part in parts)


def _merge_numstat(stats: dict[str, dict[str, int]], text: str) -> None:
    for line in text.splitlines():
        parts = line.split("\t")
        if len(parts) < 3:
            continue
        added, deleted, path = parts[0], parts[1], parts[2]
        item = stats.setdefault(path, {"additions": 0, "deletions": 0})
        if added.isdigit():
            item["additions"] += int(added)
        if deleted.isdigit():
            item["deletions"] += int(deleted)


def _untracked_file_diff(root: Path, rel: str) -> tuple[str, int] | None:
    """Untracked file without following symlinks (links yield no content)."""
    try:
        link_path = root / rel
        if link_path.is_symlink():
            return None
        path = _safe_join(root, rel)
        if path.is_symlink():
            return None
    except (OSError, ValueError):
        return None
    try:
        text = _read_text_bounded(path, max_bytes=MAX_UNTRACKED_DIFF_BYTES)
    except (OSError, UnicodeDecodeError, ValueError):
        return None
    lines = text.splitlines()
    rel_posix = rel.replace("\\", "/")
    diff = difflib.unified_diff(
        [],
        lines,
        fromfile="/dev/null",
        tofile=f"b/{rel_posix}",
        lineterm="",
    )
    body = "\n".join(diff)
    header = f"diff --git a/{rel_posix} b/{rel_posix}\nnew file mode 100644"
    return f"{header}\n{body}" if body else header, len(lines)


def _git_failed_payload() -> dict:
    return {
        "ok": False,
        "error": "git command failed; output incomplete",
        "files": [],
        "diff": "",
        "reason": GIT_REASON_FAILED,
    }


def _git_missing_payload(exc: BaseException) -> dict:
    return {
        "ok": False,
        "error": f"git unavailable: {exc}",
        "files": [],
        "diff": "",
        "reason": GIT_REASON_MISSING,
    }


def _git_exit_payload(args: list[str], returncode: int, stderr: str = "") -> dict:
    excerpt = _bounded_stderr_excerpt(stderr)
    detail = f": {excerpt}" if excerpt else ""
    return {
        "ok": False,
        "error": f"git {' '.join(args)} failed with exit {returncode}{detail}",
        "files": [],
        "diff": "",
        "reason": GIT_REASON_FAILED,
    }


def _bounded_stderr_excerpt(stderr: str) -> str:
    """Last bounded slice of git stderr for diagnosis (never unbounded)."""
    text = str(stderr or "").strip()
    if len(text) > _GIT_STDERR_EXCERPT_CHARS:
        text = text[-_GIT_STDERR_EXCERPT_CHARS:].strip()
    return " ".join(text.split())


def collect_git_changes(project: str | Path | None) -> dict:
    if not project:
        return {"ok": False, "error": "project required", "files": [], "diff": ""}
    root = Path(project).expanduser().resolve()
    if not root.exists():
        return {"ok": False, "error": "project not found", "files": [], "diff": ""}

    from codey.runtime.core import cancellation as _cancellation

    try:
        top = _run_git(root, ["rev-parse", "--show-toplevel"])
    except FileNotFoundError as exc:
        return _git_missing_payload(exc)
    except (
        OSError,
        subprocess.SubprocessError,
        _cancellation.ProcessOutputReadError,
        _cancellation.PipeDrainTimeout,
    ):
        return _git_failed_payload()
    if top.returncode != 0:
        # Only "not a git repository" on stderr confirms a non-repo; any
        # other non-zero exit is a real git failure with bounded stderr.
        if "not a git repository" in str(top.stderr or "").lower():
            return {
                "ok": False,
                "error": "not a git repository",
                "files": [],
                "diff": "",
                "reason": GIT_REASON_NOT_REPO,
            }
        return _git_exit_payload(["rev-parse", "--show-toplevel"], top.returncode, str(top.stderr or ""))
    if top.stdout_truncated:
        return {
            "ok": False,
            "error": "git rev-parse output truncated; re-run in a smaller scope",
            "files": [],
            "diff": "",
            "reason": GIT_REASON_FAILED,
        }
    git_root = Path(top.stdout.strip()).resolve()

    def _run_one(args: list[str]) -> tuple[object | None, dict | None]:
        """Run one git command: (proc, None), or (None, error to return).

        A non-zero exit is an explicit failure, never an empty change set,
        and the caller stops issuing further commands on the first error.
        """
        try:
            proc = _run_git(git_root, args)
        except FileNotFoundError as exc:
            return None, _git_missing_payload(exc)
        except (
            OSError,
            subprocess.SubprocessError,
            _cancellation.ProcessOutputReadError,
            _cancellation.PipeDrainTimeout,
        ):
            return None, _git_failed_payload()
        if proc.returncode != 0:
            return None, _git_exit_payload(args, proc.returncode, str(proc.stderr or ""))
        return proc, None

    status_proc, error = _run_one(["status", "--short"])
    if error is not None:
        return error
    assert status_proc is not None
    if status_proc.stdout_truncated:
        return {
            "ok": False,
            "error": "git status output truncated; re-run in a smaller scope",
            "files": [],
            "diff": "",
            "reason": GIT_REASON_FAILED,
        }

    files = [
        file
        for file in parse_git_status(status_proc.stdout)
        if is_displayable_change_path(file["path"])
    ]
    if not files:
        return {
            "ok": True,
            "mode": "git",
            "vcs": {"git_available": True, "is_repo": True},
            "root": str(git_root),
            "files": [],
            "changed_count": 0,
            "diff": "",
            "truncated": False,
        }

    unstaged_num, error = _run_one(["diff", "--numstat"])
    if error is not None:
        return error
    staged_num, error = _run_one(["diff", "--cached", "--numstat"])
    if error is not None:
        return error
    assert unstaged_num is not None and staged_num is not None
    if unstaged_num.stdout_truncated or staged_num.stdout_truncated:
        return {
            "ok": False,
            "error": "git numstat output truncated; re-run in a smaller scope",
            "files": [],
            "diff": "",
            "reason": GIT_REASON_FAILED,
        }

    unstaged_diff, error = _run_one(["diff", "--no-ext-diff", "--"])
    if error is not None:
        return error
    staged_diff, error = _run_one(["diff", "--cached", "--no-ext-diff", "--"])
    if error is not None:
        return error
    assert unstaged_diff is not None and staged_diff is not None

    stats: dict[str, dict[str, int]] = {}
    _merge_numstat(stats, unstaged_num.stdout)
    _merge_numstat(stats, staged_num.stdout)

    capture_truncated = bool(unstaged_diff.stdout_truncated or staged_diff.stdout_truncated)
    diff_parts: list[str] = []
    if staged_diff.stdout:
        diff_parts.append(staged_diff.stdout.rstrip())
    if unstaged_diff.stdout:
        diff_parts.append(unstaged_diff.stdout.rstrip())

    for file in files:
        path = file["path"]
        stat = stats.get(path)
        if stat:
            file.update(stat)
        if file["status"] == "??":
            untracked = _untracked_file_diff(git_root, path)
            if untracked:
                diff_text, additions = untracked
                file["additions"] = additions
                file["deletions"] = 0
                diff_parts.append(diff_text)

    diff = "\n\n".join(part for part in diff_parts if part)
    truncated = capture_truncated or len(diff) > MAX_GIT_DIFF_CHARS
    if capture_truncated:
        diff = diff[:MAX_GIT_DIFF_CHARS].rstrip() + "\n\n... diff capture truncated ..."
    elif truncated:
        diff = diff[:MAX_GIT_DIFF_CHARS].rstrip() + "\n\n... diff truncated ..."
    return {
        "ok": True,
        "mode": "git",
        "vcs": {"git_available": True, "is_repo": True},
        "root": str(git_root),
        "files": files,
        "changed_count": len(files),
        "diff": diff,
        "truncated": truncated,
    }


def _empty_snapshot_changes(
    project: str | Path | None,
    error: str | None = None,
) -> dict:
    root = str(Path(project).expanduser().resolve()) if project else ""
    return {
        "ok": True,
        "mode": "snapshot",
        "vcs": {"git_available": error != "git unavailable", "is_repo": False},
        "root": root,
        "files": [],
        "changed_count": 0,
        "diff": "",
        "truncated": False,
    }


def collect_changes(
    project: str | Path | None,
    tracker: ChangeTracker | None = None,
) -> dict:
    if not project:
        return {"ok": False, "error": "project required", "files": [], "diff": ""}
    git_data = collect_git_changes(project)
    if git_data.get("ok"):
        return git_data
    # Snapshot fallback keys off the internal reason, never the human
    # error text: only a confirmed non-repo or a missing git binary may
    # answer with a snapshot view. Failures propagate as failures.
    reason = str(git_data.get("reason", ""))
    if reason in _GIT_FALLBACK_REASONS:
        missing = reason == GIT_REASON_MISSING
        if tracker is not None:
            data = tracker.collect()
            data["vcs"] = {"git_available": not missing, "is_repo": False}
            return data
        return _empty_snapshot_changes(project, "git unavailable" if missing else None)
    return git_data


def restore_snapshot_changes(
    project: str | Path | None,
    tracker: ChangeTracker | None,
    paths: list[str] | None = None,
) -> tuple[int, dict]:
    if not project:
        return 400, {"ok": False, "error": "project required"}
    if tracker is None or not tracker.has_snapshots:
        return 404, {"ok": False, "error": "no snapshot changes to restore"}
    result = tracker.restore(paths)
    payload = {
        "ok": result.ok,
        "restored": result.restored,
        "conflicts": result.conflicts,
        "error": result.error,
    }
    return (200 if result.ok else 409), payload
