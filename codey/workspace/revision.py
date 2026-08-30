"""Durable project-state revisions for verification provenance.

This is not ``workspace.context_epoch``.  A context epoch identifies the
model-visible prompt content for one provider turn; a workspace revision
identifies the project file state that local verification observed.
"""

from __future__ import annotations

from pathlib import Path

from codey.storage.file_lock import with_file_lock
from codey.storage.local_store import project_key, read_json, write_json_atomic


SCHEMA_VERSION = 1
INITIAL_WORKSPACE_REVISION = 1
MAX_REVISION_BYTES = 8 * 1024


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


class WorkspaceRevisionStore:
    """Persist one monotonic revision counter per project under state_home."""

    def __init__(self, state_home: str | Path) -> None:
        self.state_home = Path(state_home)

    def path_for(self, project: str | Path) -> Path:
        return self.state_home / "workspace_revisions" / f"{project_key(project)}.json"

    def current(self, project: str | Path) -> int:
        return self._read_revision(self.path_for(project))

    def bump(self, project: str | Path) -> int:
        path = self.path_for(project)
        with with_file_lock(path):
            revision = self._read_revision(path) + 1
            write_json_atomic(
                path,
                {"schema_version": SCHEMA_VERSION, "revision": revision},
                max_bytes=MAX_REVISION_BYTES,
            )
            return revision

    @staticmethod
    def _read_revision(path: Path) -> int:
        payload = read_json(path, max_bytes=MAX_REVISION_BYTES)
        if not payload or payload.get("schema_version") != SCHEMA_VERSION:
            return INITIAL_WORKSPACE_REVISION
        return valid_workspace_revision(payload.get("revision")) or INITIAL_WORKSPACE_REVISION


__all__ = [
    "INITIAL_WORKSPACE_REVISION",
    "WorkspaceRevisionStore",
    "valid_workspace_revision",
    "workspace_revision_ref",
]
