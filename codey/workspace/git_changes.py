"""Bounded Git status and diff collection with explicit failure reasons."""

from __future__ import annotations

import difflib
import subprocess
from pathlib import Path
from typing import TYPE_CHECKING, NotRequired, TypedDict

if TYPE_CHECKING:
    from codey.runtime.core.cancellation import CapturedProcess
from codey.utils.change_paths import change_file_paths
from codey.workspace.paths import read_text_bounded as _read_text_bounded
from codey.workspace.paths import safe_join as _safe_join

GIT_TIMEOUT = 10
MAX_GIT_DIFF_CHARS = 240_000
MAX_GIT_OUTPUT_BYTES = 2 * 1024 * 1024
MAX_UNTRACKED_DIFF_BYTES = 120_000
GIT_REASON_NOT_REPO = "not_repo"
GIT_REASON_MISSING = "git_missing"
GIT_REASON_FAILED = "git_failed"
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


def _run_git(project: Path, args: list[str]) -> CapturedProcess:
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
    is_repo: bool = proc.returncode == 0 and proc.stdout.strip() == "true"
    return is_repo


class GitStatusRow(TypedDict):
    path: str
    status: str
    additions: int
    deletions: int
    previous_path: NotRequired[str]


def parse_git_status(short_status: str) -> list[GitStatusRow]:
    if "\x00" in (short_status or ""):
        return parse_git_status_nul(short_status)
    files: list[GitStatusRow] = []
    for line in (short_status or "").splitlines():
        if not line.strip():
            continue
        if len(line) < 4 or line[2] != " ":
            continue
        status = line[:2].strip() or "M"
        raw_path = line[3:]
        if not raw_path.strip():
            continue
        path, previous_path = change_file_paths(raw_path, "", status)
        if not path:
            continue
        item = GitStatusRow(path=path, status=status, additions=0, deletions=0)
        if previous_path:
            item["previous_path"] = previous_path
        files.append(item)
    return files


def parse_git_status_nul(nul_status: str) -> list[GitStatusRow]:
    from codey.utils.change_paths import safe_change_path

    if nul_status and not nul_status.endswith("\x00"):
        raise ValueError("incomplete Git status record")
    files: list[GitStatusRow] = []
    chunks = (nul_status or "").split("\x00")[:-1]
    index = 0
    while index < len(chunks):
        entry = chunks[index]
        index += 1
        if len(entry) < 4 or entry[2] != " ":
            raise ValueError("invalid Git status record")
        status = entry[:2].strip() or "M"
        path = safe_change_path(entry[3:])
        previous = ""
        if "R" in status or "C" in status:
            if index >= len(chunks) or not chunks[index]:
                raise ValueError("incomplete Git rename record")
            previous = safe_change_path(chunks[index])
            index += 1
            if not previous:
                raise ValueError("unsafe Git rename source")
        if not path:
            raise ValueError("unsafe Git status path")
        item = GitStatusRow(path=path, status=status, additions=0, deletions=0)
        if previous:
            item["previous_path"] = previous
        files.append(item)
    return files


def is_displayable_change_path(path: str) -> bool:
    normalized = (path or "").replace("\\", "/").strip("/")
    parts = [part for part in normalized.split("/") if part and part != "->"]
    return not any(part in CHANGE_EXCLUDED_PATH_PARTS for part in parts)


def _merge_numstat(stats: dict[str, dict[str, int]], text: str) -> None:
    if "\x00" in (text or ""):
        _merge_numstat_nul(stats, text)
        return
    for line in (text or "").splitlines():
        parts = line.split("\t")
        if len(parts) < 3:
            continue
        added, deleted = parts[0], parts[1]
        raw_path = "\t".join(parts[2:])
        path = _numstat_target(raw_path)
        if not path:
            continue
        item = stats.setdefault(path, {"additions": 0, "deletions": 0})
        if added.isascii() and added.isdigit():
            item["additions"] += int(added)
        if deleted.isascii() and deleted.isdigit():
            item["deletions"] += int(deleted)


def _merge_numstat_nul(stats: dict[str, dict[str, int]], text: str) -> None:
    from codey.utils.change_paths import safe_change_path

    if not text.endswith("\x00"):
        raise ValueError("incomplete Git numstat record")
    chunks = text.split("\x00")[:-1]
    index = 0
    while index < len(chunks):
        parts = chunks[index].split("\t", 2)
        index += 1
        if len(parts) != 3:
            raise ValueError("invalid Git numstat record")
        added, deleted, raw_path = parts
        if not raw_path:
            if index + 1 >= len(chunks) or not chunks[index] or not chunks[index + 1]:
                raise ValueError("incomplete Git numstat rename")
            raw_path = chunks[index + 1]
            index += 2
        path = safe_change_path(raw_path)
        if not path or any(value != "-" and not (value.isascii() and value.isdigit()) for value in (added, deleted)):
            raise ValueError("invalid Git numstat fields")
        item = stats.setdefault(path, {"additions": 0, "deletions": 0})
        if added != "-":
            item["additions"] += int(added)
        if deleted != "-":
            item["deletions"] += int(deleted)


def _numstat_target(raw_path: str) -> str:
    from codey.utils.change_paths import decode_git_path, safe_change_path

    text = (raw_path or "").strip()
    if not text:
        return ""
    if " => " in text:
        text = _expand_numstat_rename(text)
    decoded = decode_git_path(text.strip())
    normalized: str = safe_change_path(decoded)
    return normalized


def _expand_numstat_rename(text: str) -> str:
    before, _, after = text.partition(" => ")
    if "{" in before and "}" in after:
        open_idx = before.rfind("{")
        close_idx = after.find("}")
        if open_idx != -1 and close_idx != -1:
            prefix = before[:open_idx]
            suffix = after[close_idx + 1 :]
            new_part = after[:close_idx]
            return f"{prefix}{new_part}{suffix}"
    return after


class UntrackedFilePreview(TypedDict):
    status: str
    size: int
    diff: NotRequired[str]
    additions: NotRequired[int]


def _untracked_file_preview(root: Path, rel: str) -> UntrackedFilePreview:
    """Return an explicit preview state for one untracked file."""
    try:
        link_path = root / rel
        if link_path.is_symlink():
            return {"status": "symlink", "size": 0}
        path = _safe_join(root, rel)
        if path.is_symlink():
            return {"status": "symlink", "size": 0}
    except (OSError, ValueError):
        return {"status": "read_error", "size": 0}
    try:
        size = path.stat().st_size
    except OSError:
        return {"status": "read_error", "size": 0}
    if size > MAX_UNTRACKED_DIFF_BYTES:
        return {"status": "too_large", "size": size}
    try:
        text = _read_text_bounded(path, max_bytes=MAX_UNTRACKED_DIFF_BYTES)
    except UnicodeDecodeError:
        return {"status": "binary", "size": size}
    except (OSError, ValueError):
        return {"status": "read_error", "size": size}
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
    return {
        "status": "text",
        "size": size,
        "diff": f"{header}\n{body}" if body else header,
        "additions": len(lines),
    }


def _untracked_file_diff(root: Path, rel: str) -> tuple[str, int] | None:
    preview = _untracked_file_preview(root, rel)
    if preview.get("status") != "text":
        return None
    return preview["diff"], preview["additions"]


def _git_failed_payload() -> dict[str, object]:
    return {
        "ok": False,
        "error": "git command failed; output incomplete",
        "files": [],
        "diff": "",
        "reason": GIT_REASON_FAILED,
    }


def _git_missing_payload(exc: BaseException) -> dict[str, object]:
    return {
        "ok": False,
        "error": f"git unavailable: {exc}",
        "files": [],
        "diff": "",
        "reason": GIT_REASON_MISSING,
    }


def _git_exit_payload(args: list[str], returncode: int, stderr: str = "") -> dict[str, object]:
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
    text = (stderr or "").strip()
    if len(text) > _GIT_STDERR_EXCERPT_CHARS:
        text = text[-_GIT_STDERR_EXCERPT_CHARS:].strip()
    return " ".join(text.split())


def _run_one_git_command(
    git_root: Path,
    args: list[str],
) -> tuple[CapturedProcess | None, dict[str, object] | None]:
    """Run one git command: (proc, None), or (None, error to return).

    A non-zero exit is an explicit failure, never an empty change set,
    and the caller stops issuing further commands on the first error.
    """
    from codey.runtime.core import cancellation as _cancellation

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
        return None, _git_exit_payload(args, proc.returncode, (proc.stderr or ""))
    return proc, None


def _resolve_git_root(root: Path) -> tuple[Path | None, dict[str, object] | None]:
    from codey.runtime.core import cancellation as _cancellation

    try:
        top = _run_git(root, ["rev-parse", "--show-toplevel"])
    except FileNotFoundError as exc:
        return None, _git_missing_payload(exc)
    except (
        OSError,
        subprocess.SubprocessError,
        _cancellation.ProcessOutputReadError,
        _cancellation.PipeDrainTimeout,
    ):
        return None, _git_failed_payload()
    if top.returncode != 0:
        # Only "not a git repository" on stderr confirms a non-repo; any
        # other non-zero exit is a real git failure with bounded stderr.
        if "not a git repository" in (top.stderr or "").lower():
            return None, {
                "ok": False,
                "error": "not a git repository",
                "files": [],
                "diff": "",
                "reason": GIT_REASON_NOT_REPO,
            }
        return None, _git_exit_payload(["rev-parse", "--show-toplevel"], top.returncode, (top.stderr or ""))
    if top.stdout_truncated:
        return None, {
            "ok": False,
            "error": "git rev-parse output truncated; re-run in a smaller scope",
            "files": [],
            "diff": "",
            "reason": GIT_REASON_FAILED,
        }
    git_root = Path(top.stdout.strip()).resolve()
    if root != git_root:
        # An ignored scratch workspace inside a repository owns snapshots;
        # returning the enclosing repository would expose unrelated changes
        # while hiding the actual scratch files from its completion evidence.
        try:
            ignored = _run_git(git_root, ["check-ignore", "-q", "--", root.relative_to(git_root).as_posix()])
        except (OSError, subprocess.SubprocessError, ValueError):
            return None, _git_failed_payload()
        if ignored.returncode == 0:
            return None, {"ok": False, "error": "workspace is ignored by enclosing repository", "files": [], "diff": "", "reason": GIT_REASON_NOT_REPO}
        if ignored.returncode != 1:
            return None, _git_failed_payload()
    return git_root, None


def _load_git_status_files(git_root: Path) -> tuple[list[GitStatusRow] | None, dict[str, object] | None]:
    status_proc, error = _run_one_git_command(
        git_root, ["status", "--porcelain=v1", "-z", "--untracked-files=all"]
    )
    if error is not None:
        return None, error
    assert status_proc is not None
    if status_proc.stdout_truncated:
        return None, {
            "ok": False,
            "error": "git status output truncated; re-run in a smaller scope",
            "files": [],
            "diff": "",
            "reason": GIT_REASON_FAILED,
        }
    try:
        files = [file for file in parse_git_status(status_proc.stdout) if is_displayable_change_path(file["path"])]
    except ValueError:
        return None, _git_failed_payload()
    return files, None


def _load_git_numstat_stats(git_root: Path) -> tuple[dict[str, dict[str, int]] | None, dict[str, object] | None]:
    unstaged_num, error = _run_one_git_command(git_root, ["diff", "--numstat", "-z"])
    if error is not None:
        return None, error
    staged_num, error = _run_one_git_command(git_root, ["diff", "--cached", "--numstat", "-z"])
    if error is not None:
        return None, error
    assert unstaged_num is not None and staged_num is not None
    if unstaged_num.stdout_truncated or staged_num.stdout_truncated:
        return None, {
            "ok": False,
            "error": "git numstat output truncated; re-run in a smaller scope",
            "files": [],
            "diff": "",
            "reason": GIT_REASON_FAILED,
        }
    stats: dict[str, dict[str, int]] = {}
    try:
        _merge_numstat(stats, unstaged_num.stdout)
        _merge_numstat(stats, staged_num.stdout)
    except ValueError:
        return None, _git_failed_payload()
    return stats, None


def _load_git_diff_parts(git_root: Path) -> tuple[list[str] | None, bool, dict[str, object] | None]:
    unstaged_diff, error = _run_one_git_command(git_root, ["diff", "--no-ext-diff", "--"])
    if error is not None:
        return None, False, error
    staged_diff, error = _run_one_git_command(git_root, ["diff", "--cached", "--no-ext-diff", "--"])
    if error is not None:
        return None, False, error
    assert unstaged_diff is not None and staged_diff is not None
    capture_truncated = (unstaged_diff.stdout_truncated or staged_diff.stdout_truncated)
    diff_parts: list[str] = []
    if staged_diff.stdout:
        diff_parts.append(staged_diff.stdout.rstrip())
    if unstaged_diff.stdout:
        diff_parts.append(unstaged_diff.stdout.rstrip())
    return diff_parts, capture_truncated, None


def _apply_untracked_diffs(
    git_root: Path,
    files: list[GitStatusRow],
    stats: dict[str, dict[str, int]],
    diff_parts: list[str],
) -> None:
    for file in files:
        path = file["path"]
        stat = stats.get(path)
        if stat:
            file["additions"] = stat["additions"]
            file["deletions"] = stat["deletions"]
        if file["status"] == "??":
            untracked = _untracked_file_diff(git_root, path)
            if untracked:
                diff_text, additions = untracked
                file["additions"] = additions
                file["deletions"] = 0
                diff_parts.append(diff_text)


def _finalize_git_diff(diff_parts: list[str], capture_truncated: bool) -> tuple[str, bool]:
    diff = "\n\n".join(part for part in diff_parts if part)
    truncated = capture_truncated or len(diff) > MAX_GIT_DIFF_CHARS
    if capture_truncated:
        diff = diff[:MAX_GIT_DIFF_CHARS].rstrip() + "\n\n... diff capture truncated ..."
    elif truncated:
        diff = diff[:MAX_GIT_DIFF_CHARS].rstrip() + "\n\n... diff truncated ..."
    return diff, truncated


def collect_git_changes(project: str | Path | None) -> dict[str, object]:
    if not project:
        return {"ok": False, "error": "project required", "files": [], "diff": ""}
    root = Path(project).expanduser().resolve()
    if not root.exists():
        return {"ok": False, "error": "project not found", "files": [], "diff": ""}

    git_root, error = _resolve_git_root(root)
    if error is not None:
        return error
    assert git_root is not None

    files, error = _load_git_status_files(git_root)
    if error is not None:
        return error
    assert files is not None
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

    stats, error = _load_git_numstat_stats(git_root)
    if error is not None:
        return error
    assert stats is not None

    diff_parts, capture_truncated, error = _load_git_diff_parts(git_root)
    if error is not None:
        return error
    assert diff_parts is not None

    _apply_untracked_diffs(git_root, files, stats, diff_parts)
    diff, truncated = _finalize_git_diff(diff_parts, capture_truncated)
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


def collect_git_file_diff(project: str | Path | None, rel: str) -> dict[str, object]:
    """Collect one file's diff independently from the aggregate diff budget."""
    if not project or not rel or not is_displayable_change_path(rel):
        return {"ok": False, "error": "file path invalid", "path": rel, "status": "invalid"}
    git_root, error = _resolve_git_root(Path(project).expanduser().resolve())
    if error is not None:
        return error
    assert git_root is not None
    normalized = _safe_join(git_root, rel)
    try:
        normalized.relative_to(git_root)
    except ValueError:
        return {"ok": False, "error": "file path invalid", "path": rel, "status": "invalid"}
    status_files, error = _load_git_status_files(git_root)
    if error is not None:
        return error
    status_row = next((item for item in status_files or () if item.get("path") == rel), None)
    if status_row is None:
        return {"ok": False, "error": "file is not changed", "path": rel, "status": "missing"}
    if status_row.get("status") == "??":
        preview = _untracked_file_preview(git_root, rel)
        return {"ok": True, "path": rel, **preview}
    parts: list[str] = []
    captured = False
    for args in (["diff", "--no-ext-diff", "--", rel], ["diff", "--cached", "--no-ext-diff", "--", rel]):
        proc, command_error = _run_one_git_command(git_root, args)
        if command_error is not None:
            return command_error
        assert proc is not None
        captured = captured or proc.stdout_truncated
        if proc.stdout:
            parts.append(proc.stdout.rstrip())
    diff = "\n\n".join(parts)
    truncated = captured or len(diff) > MAX_GIT_DIFF_CHARS
    if truncated:
        diff = diff[:MAX_GIT_DIFF_CHARS].rstrip() + "\n\n... file diff truncated ..."
    status = "binary" if "Binary files" in diff else "text"
    return {
        "ok": True,
        "path": rel,
        "status": status,
        "size": normalized.stat().st_size if normalized.is_file() else 0,
        "diff": diff,
        "truncated": truncated,
    }
