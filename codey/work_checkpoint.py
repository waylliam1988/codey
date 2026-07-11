"""Bounded durable facts for one unfinished project task.

The checkpoint is execution state, not a plan or transcript.  It records only
facts observable from local tool events so a fresh provider session can resume
without treating a previous model's narrative as verified work.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from codey.local_store import (
    DEFAULT_STATE_HOME,
    delete_file,
    read_json,
    session_key,
    write_json_atomic,
)


SCHEMA_VERSION = 1
MAX_CHECKPOINT_BYTES = 64 * 1024
MAX_CHANGED_FILES = 32
MAX_SUCCESSFUL_CHECKS = 8
MAX_TASK_CHARS = 2_000
MAX_COMMAND_CHARS = 500
VALID_STATUSES = frozenset({"working", "ready_for_review", "fixing_review", "interrupted"})


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _text(value: object, limit: int) -> str:
    text = str(value or "").replace("\r\n", "\n").replace("\r", "\n").strip()
    return text[:limit].rstrip()


def _rel_path(value: object) -> str | None:
    text = str(value or "").strip().replace("\\", "/")
    path = PurePosixPath(text)
    if not text or path.is_absolute() or ".." in path.parts or len(text) > 240:
        return None
    return path.as_posix()


def _canonical_rel_path(root: Path, value: object) -> str | None:
    """Return the canonical project-relative path accepted by the runtime."""

    text = str(value or "").strip()
    if not text:
        return None
    try:
        candidate = Path(text).expanduser()
        path = candidate.resolve() if candidate.is_absolute() else (root / candidate).resolve()
        if path != root and root not in path.parents:
            return None
        rel = path.relative_to(root).as_posix()
    except (OSError, RuntimeError, ValueError):
        return None
    return _rel_path(rel)


def _file_hash(root: Path, rel: str) -> str | None:
    path = (root / rel).resolve()
    if root != path and root not in path.parents:
        return None
    if not path.exists():
        return "missing"
    if not path.is_file() or path.is_symlink():
        return None
    digest = hashlib.sha256()
    try:
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
    except OSError:
        return None
    return "sha256:" + digest.hexdigest()


@dataclass(frozen=True)
class CheckpointFile:
    path: str
    after_hash: str


@dataclass(frozen=True)
class CheckpointCheck:
    command: str
    cwd: str = "."


@dataclass(frozen=True)
class LastAction:
    tool: str
    ok: bool
    command: str = ""
    cwd: str = "."


@dataclass(frozen=True)
class WorkCheckpoint:
    run_id: str
    session_id: str
    project: str
    original_task: str
    status: str = "working"
    started_at: str = ""
    updated_at: str = ""
    changed_files: tuple[CheckpointFile, ...] = ()
    successful_checks_after_last_change: tuple[CheckpointCheck, ...] = ()
    last_action: LastAction | None = None
    stop_reason: str = ""
    workspace_changed: bool = False

    def to_payload(self) -> dict:
        payload = {
            "schema_version": SCHEMA_VERSION,
            "run_id": self.run_id,
            "session_id": self.session_id,
            "project": self.project,
            "original_task": self.original_task,
            "status": self.status,
            "started_at": self.started_at,
            "updated_at": self.updated_at,
            "changed_files": [vars(item) for item in self.changed_files],
            "successful_checks_after_last_change": [vars(item) for item in self.successful_checks_after_last_change],
            "stop_reason": self.stop_reason,
        }
        if self.last_action is not None:
            payload["last_action"] = vars(self.last_action)
        return payload


class WorkCheckpointStore:
    """Keep at most one active task checkpoint per UI session."""

    def __init__(self, state_home: str | Path = DEFAULT_STATE_HOME) -> None:
        self.state_home = Path(state_home)

    def path_for(self, session_id: str) -> Path:
        return self.state_home / "work_checkpoints" / f"{session_key(session_id)}.json"

    def load(self, session_id: str) -> WorkCheckpoint | None:
        payload = read_json(self.path_for(session_id), max_bytes=MAX_CHECKPOINT_BYTES)
        if not payload or payload.get("schema_version") != SCHEMA_VERSION:
            return None
        try:
            status = str(payload.get("status") or "")
            if status not in VALID_STATUSES or str(payload.get("session_id") or "") != session_id:
                return None
            files = []
            for item in payload.get("changed_files") or []:
                rel = _rel_path(item.get("path")) if isinstance(item, dict) else None
                digest = str(item.get("after_hash") or "") if isinstance(item, dict) else ""
                if rel and (digest == "missing" or digest.startswith("sha256:")):
                    files.append(CheckpointFile(rel, digest))
                if len(files) >= MAX_CHANGED_FILES:
                    break
            checks = []
            for item in payload.get("successful_checks_after_last_change") or []:
                if not isinstance(item, dict):
                    continue
                command = _text(item.get("command"), MAX_COMMAND_CHARS)
                cwd = _rel_path(item.get("cwd") or ".")
                if command and cwd:
                    checks.append(CheckpointCheck(command, cwd))
                if len(checks) >= MAX_SUCCESSFUL_CHECKS:
                    break
            action_value = payload.get("last_action")
            action = None
            if isinstance(action_value, dict) and action_value.get("tool"):
                action = LastAction(
                    _text(action_value.get("tool"), 40),
                    bool(action_value.get("ok")),
                    _text(action_value.get("command"), MAX_COMMAND_CHARS),
                    _rel_path(action_value.get("cwd") or ".") or ".",
                )
            project = str(Path(str(payload.get("project") or "")).expanduser().resolve())
            return WorkCheckpoint(
                run_id=_text(payload.get("run_id"), 120),
                session_id=session_id,
                project=project,
                original_task=_text(payload.get("original_task"), MAX_TASK_CHARS),
                status=status,
                started_at=_text(payload.get("started_at"), 40),
                updated_at=_text(payload.get("updated_at"), 40),
                changed_files=tuple(files),
                successful_checks_after_last_change=tuple(checks),
                last_action=action,
                stop_reason=_text(payload.get("stop_reason"), 80),
            )
        except (OSError, TypeError, ValueError):
            return None

    def save(self, checkpoint: WorkCheckpoint) -> None:
        write_json_atomic(
            self.path_for(checkpoint.session_id),
            checkpoint.to_payload(),
            max_bytes=MAX_CHECKPOINT_BYTES,
        )

    def start(self, *, run_id: str, session_id: str, project: str | Path, task: str) -> WorkCheckpoint:
        now = _now()
        checkpoint = WorkCheckpoint(
            run_id=_text(run_id, 120),
            session_id=session_id,
            project=str(Path(project).expanduser().resolve()),
            original_task=_text(task, MAX_TASK_CHARS),
            started_at=now,
            updated_at=now,
        )
        self.save(checkpoint)
        return checkpoint

    def record_edit(self, checkpoint: WorkCheckpoint, rel: str) -> WorkCheckpoint:
        root = Path(checkpoint.project).resolve()
        safe_rel = _canonical_rel_path(root, rel)
        digest = _file_hash(root, safe_rel) if safe_rel else None
        files = list(checkpoint.changed_files)
        if safe_rel and digest is not None:
            files = [item for item in files if item.path != safe_rel]
            files.append(CheckpointFile(safe_rel, digest))
        updated = replace(
            checkpoint,
            status="working",
            updated_at=_now(),
            changed_files=tuple(files[-MAX_CHANGED_FILES:]),
            successful_checks_after_last_change=(),
            last_action=LastAction("edit", True),
            stop_reason="",
            workspace_changed=False,
        )
        self.save(updated)
        return updated

    def record_run(self, checkpoint: WorkCheckpoint, *, command: str, cwd: str, ok: bool) -> WorkCheckpoint:
        safe_command = _text(command, MAX_COMMAND_CHARS)
        safe_cwd = _rel_path(cwd or ".") or "."
        checks = list(checkpoint.successful_checks_after_last_change)
        if ok and safe_command:
            check = CheckpointCheck(safe_command, safe_cwd)
            checks = [item for item in checks if item != check]
            checks.append(check)
        elif not ok:
            checks.clear()
        updated = replace(
            checkpoint,
            updated_at=_now(),
            successful_checks_after_last_change=tuple(checks[-MAX_SUCCESSFUL_CHECKS:]),
            last_action=LastAction("run", ok, safe_command, safe_cwd),
        )
        self.save(updated)
        return updated

    def set_status(self, checkpoint: WorkCheckpoint, status: str, stop_reason: str = "") -> WorkCheckpoint:
        if status not in VALID_STATUSES:
            raise ValueError(f"invalid checkpoint status: {status}")
        updated = replace(checkpoint, status=status, updated_at=_now(), stop_reason=_text(stop_reason, 80))
        self.save(updated)
        return updated

    def reconcile(self, checkpoint: WorkCheckpoint) -> WorkCheckpoint:
        root = Path(checkpoint.project).resolve()
        changed = any(_file_hash(root, item.path) != item.after_hash for item in checkpoint.changed_files)
        if not changed:
            return checkpoint
        updated = replace(
            checkpoint,
            updated_at=_now(),
            successful_checks_after_last_change=(),
            workspace_changed=True,
        )
        self.save(updated)
        return updated

    def delete(self, session_id: str) -> None:
        delete_file(self.path_for(session_id))


def render_work_checkpoint(checkpoint: WorkCheckpoint) -> str:
    lines = [
        "Local execution checkpoint (bounded local facts):",
        f"- Original task: {checkpoint.original_task}",
        f"- Previous status: {checkpoint.status}",
    ]
    if checkpoint.changed_files:
        lines.append("- Recorded changed files: " + ", ".join(item.path for item in checkpoint.changed_files))
    else:
        lines.append("- Recorded changed files: (none)")
    if checkpoint.successful_checks_after_last_change:
        checks = "; ".join(
            f"{item.command} (cwd {item.cwd})" for item in checkpoint.successful_checks_after_last_change
        )
        lines.append(f"- Successful checks after the latest recorded change: {checks}")
    else:
        lines.append("- Successful checks after the latest recorded change: (none)")
    if checkpoint.stop_reason:
        lines.append(f"- Previous stop reason: {checkpoint.stop_reason}")
    lines.append(f"- Workspace changed since checkpoint: {'yes; prior checks were invalidated' if checkpoint.workspace_changed else 'no'}")
    lines.extend([
        "This checkpoint records execution facts, not a plan or model summary.",
        "Verify the current files before editing and do not assume unfinished suggestions are correct.",
    ])
    return "\n".join(lines)
