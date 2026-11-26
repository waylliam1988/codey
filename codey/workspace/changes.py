"""Public project change collection and snapshot restoration entry points."""

from __future__ import annotations

from pathlib import Path

from codey.workspace.change_tracker import MAX_SNAPSHOT_DIFF_CHARS, ChangeTracker, RestoreResult, RestoreState, Snapshot
from codey.workspace.git_changes import (
    CHANGE_EXCLUDED_PATH_PARTS,
    GIT_REASON_FAILED,
    GIT_REASON_MISSING,
    GIT_REASON_NOT_REPO,
    GIT_TIMEOUT,
    MAX_GIT_DIFF_CHARS,
    MAX_GIT_OUTPUT_BYTES,
    MAX_UNTRACKED_DIFF_BYTES,
    GitStatusRow,
    UntrackedFilePreview,
    _run_git,
    collect_git_changes,
    collect_git_file_diff,
    is_displayable_change_path,
    is_git_repository,
    parse_git_status,
    parse_git_status_nul,
)
from codey.workspace.snapshot_store import (
    BASELINE_DIR_NAME,
    MAX_SNAPSHOT_FILE_BYTES,
    MAX_SNAPSHOT_FILES,
    MAX_SNAPSHOT_MANIFEST_BYTES,
    MAX_SNAPSHOT_TOTAL_BYTES,
    SNAPSHOT_DIR_NAME,
    SNAPSHOT_SCHEMA_VERSION,
    ProjectWriteBusy,
    SnapshotStore,
)

__all__ = [
    "BASELINE_DIR_NAME",
    "CHANGE_EXCLUDED_PATH_PARTS",
    "ChangeTracker",
    "GIT_REASON_FAILED",
    "GIT_REASON_MISSING",
    "GIT_REASON_NOT_REPO",
    "GIT_TIMEOUT",
    "GitStatusRow",
    "MAX_GIT_DIFF_CHARS",
    "MAX_GIT_OUTPUT_BYTES",
    "MAX_SNAPSHOT_DIFF_CHARS",
    "MAX_SNAPSHOT_FILES",
    "MAX_SNAPSHOT_FILE_BYTES",
    "MAX_SNAPSHOT_MANIFEST_BYTES",
    "MAX_SNAPSHOT_TOTAL_BYTES",
    "MAX_UNTRACKED_DIFF_BYTES",
    "ProjectWriteBusy",
    "RestoreResult",
    "RestoreState",
    "SNAPSHOT_DIR_NAME",
    "SNAPSHOT_SCHEMA_VERSION",
    "Snapshot",
    "SnapshotStore",
    "UntrackedFilePreview",
    "_run_git",
    "collect_changes",
    "collect_git_changes",
    "collect_git_file_diff",
    "is_displayable_change_path",
    "is_git_repository",
    "parse_git_status",
    "parse_git_status_nul",
    "restore_snapshot_changes",
]
_GIT_FALLBACK_REASONS = frozenset({GIT_REASON_NOT_REPO, GIT_REASON_MISSING})


def _empty_snapshot_changes(
    project: str | Path | None,
    error: str | None = None,
) -> dict[str, object]:
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
) -> dict[str, object]:
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
    *,
    preview: bool = False,
) -> tuple[int, dict[str, object]]:
    if not project:
        return 400, {"ok": False, "error": "project required"}
    if tracker is None or not tracker.has_snapshots:
        return 404, {"ok": False, "error": "no snapshot changes to restore"}
    if preview:
        return 200, {"ok": True, "files": [
            {"path": path, "state": state} for path, state in tracker.preview_restore(paths)
        ]}
    result = tracker.restore(paths)
    payload: dict[str, object] = {
        "ok": result.ok,
        "restored": result.restored,
        "conflicts": result.conflicts,
        "error": result.error,
    }
    return (200 if result.ok else 409), payload
