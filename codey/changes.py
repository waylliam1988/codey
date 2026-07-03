"""Git and snapshot-backed project change collection and restoration."""

from __future__ import annotations

import difflib
import hashlib
import subprocess
import threading
from dataclasses import dataclass
from pathlib import Path

from codey.local_store import (
    DEFAULT_STATE_HOME,
    delete_file,
    project_key,
    read_json,
    write_json_atomic,
)


MAX_SNAPSHOT_FILE_BYTES = 512 * 1024
MAX_SNAPSHOT_DIFF_CHARS = 240_000
MAX_SNAPSHOT_FILES = 200
MAX_SNAPSHOT_TOTAL_BYTES = 32 * 1024 * 1024
MAX_SNAPSHOT_JSON_BYTES = 64 * 1024 * 1024
SNAPSHOT_SCHEMA_VERSION = 1
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
    """Persist one bounded recovery baseline for each non-Git project."""

    def __init__(self, state_home: str | Path = DEFAULT_STATE_HOME) -> None:
        self.state_home = Path(state_home)

    def path_for(self, root: str | Path) -> Path:
        return self.state_home / "projects" / project_key(root) / "recovery.json"

    def load(self, root: str | Path) -> tuple[dict[str, str | None], dict[str, str]]:
        resolved_root = Path(root).expanduser().resolve()
        payload = read_json(
            self.path_for(resolved_root),
            max_bytes=MAX_SNAPSHOT_JSON_BYTES,
        )
        if not payload or payload.get("schema_version") != SNAPSHOT_SCHEMA_VERSION:
            return {}, {}
        raw_before = payload.get("before")
        raw_hashes = payload.get("after_hashes")
        if not isinstance(raw_before, dict) or not isinstance(raw_hashes, dict):
            return {}, {}

        before: dict[str, str | None] = {}
        total = 0
        for rel, content in raw_before.items():
            if len(before) >= MAX_SNAPSHOT_FILES or not isinstance(rel, str):
                return {}, {}
            if content is not None and not isinstance(content, str):
                return {}, {}
            try:
                path = _safe_join(resolved_root, rel)
                canonical = path.relative_to(resolved_root).as_posix()
            except (ValueError, OSError):
                return {}, {}
            if canonical != rel:
                return {}, {}
            total += len((content or "").encode("utf-8"))
            if total > MAX_SNAPSHOT_TOTAL_BYTES:
                return {}, {}
            before[rel] = content

        hashes = {
            rel: value
            for rel, value in raw_hashes.items()
            if rel in before
            and isinstance(value, str)
            and (value == "missing" or value.startswith("sha256:"))
        }
        return before, hashes

    def save(
        self,
        root: str | Path,
        before: dict[str, str | None],
        after_hashes: dict[str, str],
    ) -> None:
        path = self.path_for(root)
        if not before:
            delete_file(path)
            return
        write_json_atomic(
            path,
            {
                "schema_version": SNAPSHOT_SCHEMA_VERSION,
                "before": before,
                "after_hashes": after_hashes,
            },
            max_bytes=MAX_SNAPSHOT_JSON_BYTES,
        )

    def delete(self, root: str | Path) -> None:
        delete_file(self.path_for(root))


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

    def __init__(
        self,
        root: str | Path,
        store: SnapshotStore | None = None,
    ) -> None:
        self.root = Path(root).expanduser().resolve()
        self.store = store
        self._store_lock = threading.Lock()
        if store is None:
            self._before: dict[str, str | None] = {}
            self._after_hashes: dict[str, str] = {}
        else:
            self._before, self._after_hashes = store.load(self.root)

    @property
    def has_snapshots(self) -> bool:
        return bool(self._before)

    def _persist(self) -> None:
        with self._store_lock:
            if self.store is not None:
                self.store.save(self.root, self._before, self._after_hashes)

    def disable_persistence(self) -> None:
        with self._store_lock:
            self.store = None

    def _validate_capacity(self, rel: str, content: str | None) -> None:
        if rel not in self._before and len(self._before) >= MAX_SNAPSHOT_FILES:
            raise ValueError("snapshot file limit reached")
        total = sum(len((value or "").encode("utf-8")) for value in self._before.values())
        if rel not in self._before:
            total += len((content or "").encode("utf-8"))
        if total > MAX_SNAPSHOT_TOTAL_BYTES:
            raise ValueError("snapshot size limit reached")

    def capture_before(self, rel: str) -> None:
        path = _safe_join(self.root, rel)
        rel_posix = path.relative_to(self.root).as_posix()
        if rel_posix in self._before:
            return
        before = _read_text_or_none(path)
        self._validate_capacity(rel_posix, before)
        self._before[rel_posix] = before
        try:
            self._persist()
        except Exception:
            self._before.pop(rel_posix, None)
            raise

    def capture_after(self, rel: str) -> None:
        path = _safe_join(self.root, rel)
        rel_posix = path.relative_to(self.root).as_posix()
        if rel_posix not in self._before:
            return
        try:
            self._after_hashes[rel_posix] = _path_hash(path)
            self._persist()
        except (OSError, UnicodeDecodeError, ValueError):
            pass

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
                continue
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
            diff_text = diff_text[:MAX_SNAPSHOT_DIFF_CHARS].rstrip() + "\n\n... diff truncated ..."
        changed_paths = {snapshot.path for snapshot in snapshots}
        for rel in list(self._before):
            if rel in changed_paths:
                continue
            try:
                current = _read_text_or_none(_safe_join(self.root, rel))
            except (OSError, UnicodeDecodeError, ValueError):
                continue
            if current == self._before[rel]:
                self._before.pop(rel, None)
                self._after_hashes.pop(rel, None)
        try:
            self._persist()
        except (OSError, ValueError):
            pass
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
            try:
                current_hash = _path_hash(path)
            except (OSError, ValueError):
                conflicts.append(rel)
                continue
            if _content_hash(before) == current_hash:
                self._before.pop(rel, None)
                self._after_hashes.pop(rel, None)
                continue
            expected_hash = self._after_hashes.get(rel)
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
                    path.parent.mkdir(parents=True, exist_ok=True)
                    path.write_text(before, encoding="utf-8")
                except OSError:
                    conflicts.append(rel)
                    continue
            restored.append(rel)
            self._before.pop(rel, None)
            self._after_hashes.pop(rel, None)

        try:
            self._persist()
        except (OSError, ValueError):
            pass
        return RestoreResult(not conflicts, restored, conflicts, None if not conflicts else "restore conflict")


def _run_git(project: Path, args: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-C", str(project), *args],
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
        path = line[3:].strip() if len(line) > 3 else line[2:].strip()
        files.append({"path": path, "status": status, "additions": 0, "deletions": 0})
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
    lines = text.splitlines(keepends=True)
    rel_posix = rel.replace("\\", "/")
    diff = difflib.unified_diff(
        [],
        lines,
        fromfile="/dev/null",
        tofile=f"b/{rel_posix}",
        lineterm="",
    )
    return "\n".join(diff), len(text.splitlines())


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
