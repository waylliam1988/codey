"""Git and snapshot-backed project change collection and restoration."""

from __future__ import annotations

import difflib
import hashlib
import shutil
import subprocess
import threading
from dataclasses import dataclass
from pathlib import Path

from codey.storage.atomic_io import write_bytes_atomic, write_text_atomic
from codey.storage.file_lock import with_file_lock
from codey.storage.local_store import (
    DEFAULT_STATE_HOME,
    delete_file,
    project_key,
    read_json,
    write_json_atomic,
)
from codey.utils.change_paths import change_file_paths


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
MAX_UNTRACKED_DIFF_BYTES = 120_000
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
        return self.dir_for(resolved_root).parent / ".snapshots.lock"

    def _baseline_path(self, root: str | Path, rel: str) -> Path:
        digest = hashlib.sha256(rel.encode("utf-8")).hexdigest()[:32]
        return self.dir_for(root) / BASELINE_DIR_NAME / f"{digest}.txt"

    def load(self, root: str | Path) -> tuple[dict[str, str | None], dict[str, str]]:
        resolved_root = Path(root).expanduser().resolve()
        with with_file_lock(self._lock_target(resolved_root)):
            manifest_path = self.path_for(resolved_root)
            payload = read_json(
                manifest_path,
                max_bytes=MAX_SNAPSHOT_MANIFEST_BYTES,
            )
            if not payload or payload.get("schema_version") != SNAPSHOT_SCHEMA_VERSION:
                return {}, {}
            raw_files = payload.get("files")
            if not isinstance(raw_files, dict):
                return {}, {}

            before: dict[str, str | None] = {}
            hashes: dict[str, str] = {}
            total = 0
            for rel, entry in raw_files.items():
                if len(before) >= MAX_SNAPSHOT_FILES or not isinstance(rel, str):
                    return {}, {}
                if not isinstance(entry, dict):
                    return {}, {}
                try:
                    path = _safe_join(resolved_root, rel)
                    canonical = path.relative_to(resolved_root).as_posix()
                except (ValueError, OSError):
                    return {}, {}
                if canonical != rel:
                    return {}, {}
                content: str | None
                if entry.get("baseline") is None:
                    content = None
                else:
                    try:
                        body = self._baseline_path(resolved_root, rel).read_text(
                            encoding="utf-8"
                        )
                    except (OSError, UnicodeDecodeError):
                        return {}, {}
                    if len(body.encode("utf-8")) > MAX_SNAPSHOT_FILE_BYTES:
                        return {}, {}
                    content = body
                total += len((content or "").encode("utf-8"))
                if total > MAX_SNAPSHOT_TOTAL_BYTES:
                    return {}, {}
                before[rel] = content

                digest = entry.get("after_hash")
                if digest is not None and not (
                    isinstance(digest, str)
                    and (digest == "missing" or digest.startswith("sha256:"))
                ):
                    return {}, {}
                if isinstance(digest, str):
                    hashes[rel] = digest
            return before, hashes

    def put_baseline(
        self,
        root: str | Path,
        rel: str,
        content: str | None,
    ) -> None:
        """Record (or replace) one file's recovery baseline."""

        resolved_root = Path(root).expanduser().resolve()
        body_path = self._baseline_path(resolved_root, rel)

        with with_file_lock(self._lock_target(resolved_root)):
            written_body = False
            try:
                if content is None:
                    _remove_file(body_path)
                else:
                    _write_bytes_atomic(body_path, content.encode("utf-8"))
                    written_body = True
                self._update_manifest_locked(
                    resolved_root,
                    rel,
                    lambda entry: {**entry, "baseline": None if content is None else body_path.name},
                )
            except Exception:
                if written_body:
                    _remove_file(body_path)
                raise

    def set_after_hash(self, root: str | Path, rel: str, digest: str) -> None:
        resolved_root = Path(root).expanduser().resolve()
        with with_file_lock(self._lock_target(resolved_root)):
            self._update_manifest_locked(
                resolved_root,
                rel,
                lambda entry: {**entry, "after_hash": digest},
            )

    def remove(self, root: str | Path, rel: str) -> None:
        """Drop one file from the snapshot; deletes the store when empty."""

        resolved_root = Path(root).expanduser().resolve()
        body_path = self._baseline_path(resolved_root, rel)
        manifest_path = self.path_for(resolved_root)

        with with_file_lock(self._lock_target(resolved_root)):
            _remove_file(body_path)
            _remove_dir_if_empty(body_path.parent)

            payload = read_json(manifest_path, max_bytes=MAX_SNAPSHOT_MANIFEST_BYTES)
            files = payload.get("files") if isinstance(payload, dict) else None
            if not isinstance(files, dict) or rel not in files:
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

    def delete(self, root: str | Path) -> None:
        resolved_root = Path(root).expanduser().resolve()
        with with_file_lock(self._lock_target(resolved_root)):
            try:
                shutil.rmtree(self.dir_for(resolved_root))
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
        payload = read_json(manifest_path, max_bytes=MAX_SNAPSHOT_MANIFEST_BYTES)
        files = payload.get("files") if isinstance(payload, dict) else None
        if not isinstance(files, dict):
            files = {}
        entry = files.get(rel)
        entry = dict(entry) if isinstance(entry, dict) else {}
        files[rel] = mutate(entry)
        write_json_atomic(
            manifest_path,
            {"schema_version": SNAPSHOT_SCHEMA_VERSION, "files": files},
            max_bytes=MAX_SNAPSHOT_MANIFEST_BYTES,
        )


def _write_bytes_atomic(path: Path, data: bytes) -> None:
    write_bytes_atomic(path, data)


def _remove_file(path: Path) -> None:
    try:
        path.unlink(missing_ok=True)
    except OSError:
        pass


def _remove_dir_if_empty(directory: Path) -> None:
    try:
        directory.rmdir()
    except OSError:
        pass


def _diff_and_counts(path: str, before: str | None, after: str | None) -> tuple[str, int, int]:
    # splitlines() without keepends + lineterm="": keeping line endings here
    # made every content line double-spaced in the rendered diff.
    before_lines = [] if before is None else before.splitlines()
    after_lines = [] if after is None else after.splitlines()
    fromfile = "/dev/null" if before is None else f"a/{path}"
    tofile = "/dev/null" if after is None else f"b/{path}"
    diff_lines = list(difflib.unified_diff(before_lines, after_lines, fromfile=fromfile, tofile=tofile, lineterm=""))
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
    """Record first-write baselines and render diffs against current files.

    All baseline state (``_before`` / ``_after_hashes``) is guarded by one
    reentrant lock: UI polling collects while a run captures, so a collect
    must never observe a half-updated baseline set -- and must never mutate
    it either. ``collect()`` is read-only by default; call
    :meth:`prune_clean` explicitly (after a run reaches a terminal state)
    to drop baselines whose file is back to its original content.
    """

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
            if rel_posix in self._before:
                return
        before = _read_text_or_none(path)
        added = len((before or "").encode("utf-8"))
        # The file read above happens outside the lock, so another thread may
        # have captured this rel in the meantime. Re-check membership before
        # mutating anything: exactly one thread wins and pays the byte cost.
        with self._lock:
            if rel_posix in self._before:
                return
            self._validate_capacity_locked(rel_posix, before)
            self._before[rel_posix] = before
            self._total_bytes += added
        try:
            with self._lock:
                store = self.store
            if store is not None:
                store.put_baseline(self.root, rel_posix, before)
        except Exception:
            with self._lock:
                # Roll back only our own write; prune/restore may already have
                # removed it, and a concurrent re-capture must survive.
                if rel_posix in self._before and self._before[rel_posix] == before:
                    del self._before[rel_posix]
                    self._total_bytes -= added
                store = self.store
            if store is not None:
                try:
                    store.remove(self.root, rel_posix)
                except Exception:
                    pass
            raise

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
            try:
                store.set_after_hash(self.root, rel_posix, digest)
            except (OSError, ValueError):
                pass

    def snapshots(self, paths: list[str] | None = None) -> list[Snapshot]:
        with self._lock:
            selected = set(paths or self._before.keys())
            tracked = sorted(self._before)
        items: list[Snapshot] = []
        for rel in tracked:
            if rel not in selected:
                continue
            path = _safe_join(self.root, rel)
            try:
                after = _read_text_or_none(path)
            except (OSError, UnicodeDecodeError, ValueError):
                continue
            with self._lock:
                before = self._before[rel]
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
            diff, additions, deletions = _diff_and_counts(snapshot.path, snapshot.before, snapshot.after)
            files.append({
                "path": snapshot.path,
                "status": _status_for(snapshot.before, snapshot.after),
                "additions": additions,
                "deletions": deletions,
            })
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
                current = _read_text_or_none(_safe_join(self.root, rel))
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
        content = self._before.pop(rel, None)
        self._after_hashes.pop(rel, None)
        self._total_bytes -= len((content or "").encode("utf-8"))
        with self._lock:
            store = self.store
        if store is not None:
            try:
                store.remove(self.root, rel)
            except (OSError, ValueError):
                pass

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
            if before is None:
                try:
                    if path.exists():
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


def _run_git(project: Path, args: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-c", "core.quotePath=false", "-C", str(project), *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=GIT_TIMEOUT,
        check=False,
    )


def is_git_repository(project: str | Path) -> bool:
    try:
        proc = _run_git(
            Path(project).expanduser().resolve(),
            ["rev-parse", "--is-inside-work-tree"],
        )
    except (FileNotFoundError, OSError, subprocess.SubprocessError):
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
    path = (root / rel).resolve()
    try:
        if not path.is_file() or path.stat().st_size > MAX_UNTRACKED_DIFF_BYTES:
            return None
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
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


def collect_git_changes(project: str | Path | None) -> dict:
    if not project:
        return {"ok": False, "error": "project required", "files": [], "diff": ""}
    root = Path(project).expanduser().resolve()
    if not root.exists():
        return {"ok": False, "error": "project not found", "files": [], "diff": ""}

    try:
        top = _run_git(root, ["rev-parse", "--show-toplevel"])
    except (OSError, subprocess.TimeoutExpired) as exc:
        return {"ok": False, "error": f"git unavailable: {exc}", "files": [], "diff": ""}
    if top.returncode != 0:
        return {"ok": False, "error": "not a git repository", "files": [], "diff": ""}
    git_root = Path(top.stdout.strip()).resolve()

    try:
        status_proc = _run_git(git_root, ["status", "--short"])
        unstaged_num = _run_git(git_root, ["diff", "--numstat"])
        staged_num = _run_git(git_root, ["diff", "--cached", "--numstat"])
        unstaged_diff = _run_git(git_root, ["diff", "--no-ext-diff", "--"])
        staged_diff = _run_git(git_root, ["diff", "--cached", "--no-ext-diff", "--"])
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": "git command timed out", "files": [], "diff": ""}

    files = [
        file
        for file in parse_git_status(status_proc.stdout)
        if is_displayable_change_path(file["path"])
    ]
    stats: dict[str, dict[str, int]] = {}
    _merge_numstat(stats, unstaged_num.stdout)
    _merge_numstat(stats, staged_num.stdout)

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
    truncated = len(diff) > MAX_GIT_DIFF_CHARS
    if truncated:
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
    if tracker is not None:
        data = tracker.collect()
        data["vcs"] = {
            "git_available": not str(git_data.get("error", "")).startswith("git unavailable"),
            "is_repo": False,
        }
        return data
    error = str(git_data.get("error", ""))
    if error == "not a git repository" or error.startswith("git unavailable"):
        return _empty_snapshot_changes(
            project,
            "git unavailable" if error.startswith("git unavailable") else None,
        )
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
