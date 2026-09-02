"""Durable project-state revisions for verification provenance.

This is not ``workspace.context_epoch``.  A context epoch identifies the
model-visible prompt content for one provider turn; a workspace revision
identifies the project file state that local verification observed.  The
revision counter is paired with a workspace fingerprint so a reused green
check cannot silently survive an out-of-band file change.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

from codey.storage.file_lock import with_file_lock
from codey.storage.local_store import project_key, write_json_atomic
from codey.workspace.bounded_scan import BoundedScanBudget, iter_bounded_files
from codey.workspace.config import path_matches_ignored_prefix


SCHEMA_VERSION = 1
INITIAL_WORKSPACE_REVISION = 1
MAX_REVISION_BYTES = 8 * 1024
MAX_FINGERPRINT_FILES = 5_000
MAX_FINGERPRINT_FILE_BYTES = 2 * 1024 * 1024
MAX_FINGERPRINT_CONTENT_BYTES = 64 * 1024 * 1024
WORKSPACE_FINGERPRINT_RE = re.compile(r"^sha256:[0-9a-f]{64}$")

FINGERPRINT_EXCLUDED_DIRS = frozenset({
    ".git",
    ".hg",
    ".svn",
    ".codey",
    ".idea",
    ".vscode",
    ".venv",
    "venv",
    "node_modules",
    "__pycache__",
    ".pytest_cache",
    ".mypy_cache",
    ".ruff_cache",
    ".next",
    ".turbo",
    ".e2e-artifacts",
    ".agents",
    ".codex",
    ".github",
    "dist",
    "build",
    "target",
    "coverage",
    "codey.egg-info",
    "reference-projects",
})


class WorkspaceRevisionError(Exception):
    """Base class for workspace revision failures."""


class WorkspaceRevisionCorruption(WorkspaceRevisionError):
    """Persisted workspace revision state cannot be trusted."""


@dataclass(frozen=True)
class WorkspaceState:
    revision: int = INITIAL_WORKSPACE_REVISION
    fingerprint: str = ""


def valid_workspace_revision(value: object) -> int:
    if isinstance(value, bool):
        return 0
    try:
        revision = int(value)
    except (TypeError, ValueError):
        return 0
    return revision if revision >= INITIAL_WORKSPACE_REVISION else 0


def workspace_revision_ref(project: str | Path, revision: object) -> str:
    number = valid_workspace_revision(revision)
    if number == 0:
        return ""
    return f"workspace_revision:{project_key(project)}:{number}"


def valid_workspace_fingerprint(value: object) -> str:
    text = str(value or "").strip()
    return text if WORKSPACE_FINGERPRINT_RE.fullmatch(text) else ""


def workspace_fingerprint_ref(project: str | Path, fingerprint: object) -> str:
    text = valid_workspace_fingerprint(fingerprint)
    if not text:
        return ""
    return f"workspace_fingerprint:{project_key(project)}:{text.removeprefix('sha256:')}"


def workspace_fingerprint(
    project: str | Path,
    *,
    ignored_paths: Iterable[str] = (),
) -> str:
    """Return a bounded fingerprint for the current project file state."""

    try:
        root = Path(project).expanduser().resolve()
    except (OSError, RuntimeError):
        return ""
    if not root.is_dir():
        return ""
    digest = hashlib.sha256()
    content_bytes = 0
    budget = BoundedScanBudget(
        max_files=MAX_FINGERPRINT_FILES,
        max_dirs=500,
        max_dir_entries=1_000,
    )

    def allowed(path: Path) -> bool:
        try:
            rel = path.resolve().relative_to(root).as_posix()
        except (OSError, RuntimeError, ValueError):
            return False
        return bool(rel and not path_matches_ignored_prefix(rel, ignored_paths))

    for child in iter_bounded_files(
        root,
        excluded_dirs=set(FINGERPRINT_EXCLUDED_DIRS),
        budget=budget,
        allow_dir=allowed,
        allow_file=allowed,
    ):
        try:
            rel = child.resolve().relative_to(root).as_posix()
            stat = child.stat()
        except (OSError, RuntimeError, ValueError):
            return ""
        digest.update(b"file\0")
        digest.update(rel.encode("utf-8", errors="surrogateescape"))
        digest.update(b"\0")
        digest.update(str(stat.st_size).encode("ascii"))
        digest.update(b"\0")
        if (
            stat.st_size <= MAX_FINGERPRINT_FILE_BYTES
            and content_bytes + stat.st_size <= MAX_FINGERPRINT_CONTENT_BYTES
        ):
            content_digest = _file_content_digest(child)
            if not content_digest:
                return ""
            content_bytes += stat.st_size
            digest.update(b"content\0")
            digest.update(content_digest.encode("ascii"))
        else:
            digest.update(b"metadata\0")
            digest.update(str(stat.st_mtime_ns).encode("ascii"))
        digest.update(b"\0")
    if budget.limited:
        return ""
    return "sha256:" + digest.hexdigest()


class WorkspaceRevisionStore:
    """Persist one monotonic revision counter per project under state_home."""

    def __init__(self, state_home: str | Path) -> None:
        self.state_home = Path(state_home)

    def path_for(self, project: str | Path) -> Path:
        return self.state_home / "workspace_revisions" / f"{project_key(project)}.json"

    def current(self, project: str | Path) -> int:
        return self.current_state(project).revision

    def current_state(
        self,
        project: str | Path,
        *,
        ignored_paths: Iterable[str] = (),
    ) -> WorkspaceState:
        path = self.path_for(project)
        try:
            if not path.exists():
                return WorkspaceState(
                    revision=INITIAL_WORKSPACE_REVISION,
                    fingerprint=workspace_fingerprint(project, ignored_paths=ignored_paths),
                )
        except OSError:
            return WorkspaceState(
                revision=INITIAL_WORKSPACE_REVISION,
                fingerprint=workspace_fingerprint(project, ignored_paths=ignored_paths),
            )
        with with_file_lock(path):
            return WorkspaceState(
                revision=self._read_revision_unlocked(path),
                fingerprint=workspace_fingerprint(project, ignored_paths=ignored_paths),
            )

    def bump(self, project: str | Path) -> int:
        return self.bump_state(project).revision

    def bump_state(
        self,
        project: str | Path,
        *,
        ignored_paths: Iterable[str] = (),
    ) -> WorkspaceState:
        path = self.path_for(project)
        with with_file_lock(path):
            revision = self._read_revision_unlocked(path) + 1
            write_json_atomic(
                path,
                {"schema_version": SCHEMA_VERSION, "revision": revision},
                max_bytes=MAX_REVISION_BYTES,
            )
            return WorkspaceState(
                revision=revision,
                fingerprint=workspace_fingerprint(project, ignored_paths=ignored_paths),
            )

    @staticmethod
    def _read_revision_unlocked(path: Path) -> int:
        try:
            if not path.exists():
                return INITIAL_WORKSPACE_REVISION
            if not path.is_file():
                raise WorkspaceRevisionCorruption("workspace revision path is not a file")
            if path.stat().st_size > MAX_REVISION_BYTES:
                raise WorkspaceRevisionCorruption("workspace revision exceeds size limit")
            payload = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return INITIAL_WORKSPACE_REVISION
        except WorkspaceRevisionCorruption:
            raise
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise WorkspaceRevisionCorruption("workspace revision is unreadable") from exc
        if not isinstance(payload, dict):
            raise WorkspaceRevisionCorruption("workspace revision root must be an object")
        if payload.get("schema_version") != SCHEMA_VERSION:
            raise WorkspaceRevisionCorruption("unsupported workspace revision schema")
        revision = valid_workspace_revision(payload.get("revision"))
        if not revision:
            raise WorkspaceRevisionCorruption("workspace revision value is invalid")
        return revision


def _file_content_digest(path: Path) -> str:
    digest = hashlib.sha256()
    try:
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
    except OSError:
        return ""
    return digest.hexdigest()


__all__ = [
    "FINGERPRINT_EXCLUDED_DIRS",
    "INITIAL_WORKSPACE_REVISION",
    "WorkspaceRevisionCorruption",
    "WorkspaceRevisionError",
    "WorkspaceRevisionStore",
    "WorkspaceState",
    "valid_workspace_fingerprint",
    "valid_workspace_revision",
    "workspace_fingerprint",
    "workspace_fingerprint_ref",
    "workspace_revision_ref",
]
