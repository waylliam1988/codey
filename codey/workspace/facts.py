"""Bounded project facts learned only from successful local checks."""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from codey.policies.command_line import split_run_command
from codey.storage.file_lock import with_file_lock
from codey.storage.local_store import (
    DEFAULT_STATE_HOME,
    StoreCorruption,
    backup_corrupt_file,
    project_key,
    read_json_strict,
    write_json_atomic,
)
from codey.types.json_value import JsonObject
from codey.utils.change_paths import safe_change_path
from codey.utils.object_fields import object_fields, sequence_items

SCHEMA_VERSION = 1
MAX_VERIFIED_COMMANDS = 8
MAX_SUCCESSFUL_CHANGES = 6
MAX_COMMAND_CHARS = 300
MAX_TASK_EXCERPT_CHARS = 240
MAX_CHANGE_FILES = 12
SENSITIVE_COMMAND_RE = re.compile(
    r"(?i)(api[-_]?key|authorization|cookie|passwd|password|secret|token)"
)
CHECK_MARKERS = {
    "build",
    "check",
    "lint",
    "py_compile",
    "pytest",
    "test",
    "tests",
    "typecheck",
    "unittest",
    "vet",
}


@dataclass(frozen=True)
class VerifiedCommand:
    command: str
    cwd: str = "."
    kind: str = "run"
    entry_file: str = ""

    def to_payload(self) -> JsonObject:
        payload: JsonObject = {"command": self.command, "cwd": self.cwd, "kind": self.kind}
        if self.entry_file:
            payload["entry_file"] = self.entry_file
        return payload


@dataclass(frozen=True)
class SuccessfulCheck:
    command: str
    cwd: str = "."

    def to_payload(self) -> JsonObject:
        return {"command": self.command, "cwd": self.cwd}


@dataclass(frozen=True)
class ProjectFactSource:
    session_id: str
    run_id: str

    @classmethod
    def from_payload(cls, value: object) -> ProjectFactSource | None:
        if not object_fields(value):
            return None
        session_id, run_id = value.get("session_id"), value.get("run_id")
        if not isinstance(session_id, str) or not session_id or not isinstance(run_id, str) or not run_id:
            return None
        return cls(session_id, run_id)

    def to_payload(self) -> JsonObject:
        return {"session_id": self.session_id, "run_id": self.run_id}


@dataclass(frozen=True)
class SuccessfulChange:
    task: str
    files: tuple[str, ...] = ()
    checks: tuple[SuccessfulCheck, ...] = ()
    receipt: str = ""
    source: ProjectFactSource | None = None

    def to_payload(self) -> JsonObject:
        payload: JsonObject = {
            "task": self.task,
            "files": list(self.files),
            "checks": [item.to_payload() for item in self.checks],
        }
        if self.receipt:
            payload["receipt"] = self.receipt
        if self.source is not None:
            payload["source"] = self.source.to_payload()
        return payload


@dataclass(frozen=True)
class ProjectFacts:
    commands: tuple[VerifiedCommand, ...] = ()
    successful_changes: tuple[SuccessfulChange, ...] = ()


def _safe_cwd(value: str) -> str | None:
    text = (value or ".").strip().replace("\\", "/") or "."
    return safe_change_path(text, allow_root=True) or None


def _verified_command(command: object, cwd: object) -> VerifiedCommand | None:
    if not isinstance(command, str) or not isinstance(cwd, str):
        return None
    text = (command or "").strip()
    safe_cwd = _safe_cwd(cwd)
    if (
        not text
        or len(text) > MAX_COMMAND_CHARS
        or "\n" in text
        or "\r" in text
        or safe_cwd is None
        or SENSITIVE_COMMAND_RE.search(text)
    ):
        return None
    try:
        argv = split_run_command(text)
    except ValueError:
        return None
    if not argv:
        return None

    lowered = {Path(part).name.lower() for part in argv}
    kind = "check" if lowered & CHECK_MARKERS else "run"
    entry_file = ""
    executable = Path(argv[0]).name.lower()
    if kind == "run" and executable in {"python", "python.exe", "py", "py.exe"}:
        scripts = [part for part in argv[1:] if part.lower().endswith(".py")]
        if len(scripts) != 1 or len(argv) != 2:
            return None
        entry_file = scripts[0].replace("\\", "/")
    return VerifiedCommand(text, safe_cwd, kind, entry_file)


def _safe_task_excerpt(value: object) -> str:
    text = str(value or "").replace("\r\n", "\n").replace("\r", "\n").strip()
    text = " ".join(line.strip() for line in text.splitlines() if line.strip())
    if len(text) > MAX_TASK_EXCERPT_CHARS:
        text = text[:MAX_TASK_EXCERPT_CHARS].rstrip() + "..."
    return text


def _safe_rel_path(value: object) -> str | None:
    text = str(value or "").strip().replace("\\", "/")
    if not text or len(text) > 240:
        return None
    return safe_change_path(text, allow_root=True) or None


def _safe_receipt(value: object) -> str:
    text = str(value or "").replace("\r\n", "\n").replace("\r", "\n").strip()
    text = " ".join(line.strip() for line in text.splitlines() if line.strip())
    return text[:240].rstrip()


def _successful_check_from_object(value: object) -> SuccessfulCheck | None:
    # Cold start: persisted checks are always {command, cwd} dicts; in-memory
    # checks are CheckEvidence-like objects. Legacy plain-string checks are
    # rejected (fail closed) instead of being coerced.
    if isinstance(value, str):
        return None
    command: object
    cwd: object
    if object_fields(value):
        command = value.get("command", "")
        cwd = value.get("cwd", ".")
    else:
        command = getattr(value, "command", "")
        cwd = getattr(value, "cwd", ".")
    fact = _verified_command(command, cwd)
    if fact is None or fact.kind != "check":
        return None
    return SuccessfulCheck(fact.command, fact.cwd)


def _successful_change(
    *,
    task: object,
    files: Sequence[object],
    checks: Sequence[object],
    receipt: object = "",
    source: ProjectFactSource | None = None,
) -> SuccessfulChange | None:
    task_excerpt = _safe_task_excerpt(task)
    if not task_excerpt:
        return None
    safe_files = tuple(
        path
        for path in (_safe_rel_path(item) for item in files)
        if path is not None
    )[:MAX_CHANGE_FILES]
    safe_checks = tuple(
        item
        for item in (_successful_check_from_object(check) for check in checks)
        if item is not None
    )[:MAX_VERIFIED_COMMANDS]
    if not safe_files or not safe_checks:
        return None
    return SuccessfulChange(
        task=task_excerpt,
        files=safe_files,
        checks=safe_checks,
        receipt=_safe_receipt(receipt),
        source=source,
    )


def _successful_change_from_payload(value: object) -> SuccessfulChange | None:
    if not object_fields(value):
        return None
    raw_files = value.get("files")
    files = raw_files if sequence_items(raw_files) else ()
    raw_checks = value.get("checks")
    checks = raw_checks if sequence_items(raw_checks) else ()
    return _successful_change(
        task=value.get("task"),
        files=files,
        checks=checks,
        receipt=value.get("receipt", ""),
        source=ProjectFactSource.from_payload(value.get("source")),
    )


class ProjectFactsStore:
    """Keep one small, overwritable set of verified facts per project."""

    def __init__(self, state_home: str | Path = DEFAULT_STATE_HOME) -> None:
        self.state_home = Path(state_home)

    def path_for(self, project: str | Path) -> Path:
        return self.state_home / "projects" / project_key(project) / "facts.json"

    def load(self, project: str | Path) -> ProjectFacts:
        path = self.path_for(project)
        try:
            payload = read_json_strict(path)
        except StoreCorruption:
            backup_corrupt_file(path)
            return ProjectFacts()
        if not isinstance(payload, dict):
            return ProjectFacts()
        if not payload or type(payload.get("schema_version")) is not int or payload.get("schema_version") != SCHEMA_VERSION:
            return ProjectFacts()
        commands: list[VerifiedCommand] = []
        raw_commands = payload.get("commands") or ()
        for item in raw_commands if sequence_items(raw_commands) else ():
            if not object_fields(item):
                continue
            fact = _verified_command(item.get("command", ""), item.get("cwd", "."))
            if fact is None:
                continue
            commands.append(fact)
        successful_changes: list[SuccessfulChange] = []
        raw_changes = payload.get("successful_changes") or ()
        for item in raw_changes if sequence_items(raw_changes) else ():
            change = _successful_change_from_payload(item)
            if change is not None:
                successful_changes.append(change)
        return ProjectFacts(
            tuple(commands[-MAX_VERIFIED_COMMANDS:]),
            tuple(successful_changes[-MAX_SUCCESSFUL_CHANGES:]),
        )

    def record_success(self, project: str | Path, cwd: str, command: str) -> bool:
        fact = _verified_command(command, cwd)
        if fact is None:
            return False
        path = self.path_for(project)
        with with_file_lock(path):
            facts = self.load(project)
            commands = tuple(item for item in facts.commands if (item.command, item.cwd) != (fact.command, fact.cwd))
            self._write(path, ProjectFacts((*commands, fact)[-MAX_VERIFIED_COMMANDS:], facts.successful_changes))
        return True

    def record_successful_change(
        self,
        project: str | Path,
        *,
        task: object,
        files: Sequence[object],
        checks: Sequence[object] = (),
        receipt: object = "",
        source: ProjectFactSource | None = None,
    ) -> bool:
        change = _successful_change(
            task=task,
            files=files,
            checks=checks or (),
            receipt=receipt,
            source=source,
        )
        if change is None:
            return False
        path = self.path_for(project)
        with with_file_lock(path):
            facts = self.load(project)
            identity = (change.task, change.files, change.checks)
            changes = tuple(item for item in facts.successful_changes
                            if (item.task, item.files, item.checks) != identity)
            self._write(path, ProjectFacts(facts.commands, (*changes, change)[-MAX_SUCCESSFUL_CHANGES:]))
        return True

    @staticmethod
    def _write(path: Path, facts: ProjectFacts) -> None:
        write_json_atomic(path, {
            "schema_version": SCHEMA_VERSION,
            "commands": [item.to_payload() for item in facts.commands],
            "successful_changes": [item.to_payload() for item in facts.successful_changes],
        })

    def render(self, project: str | Path) -> str:
        facts = self.load(project)
        root = Path(project).expanduser().resolve()
        lines = []
        for fact in facts.commands:
            if fact.entry_file:
                entry = (root / fact.cwd / fact.entry_file).resolve()
                if root not in entry.parents or not entry.is_file():
                    continue
            label = "successful check" if fact.kind == "check" else "successful run"
            suffix = f" from {fact.cwd}" if fact.cwd != "." else ""
            lines.append(f"- {label}{suffix}: {fact.command}")
        for change in facts.successful_changes:
            files = ", ".join(change.files[:4])
            if len(change.files) > 4:
                files += ", ..."
            checks = "; ".join(_format_check(item) for item in change.checks[:2])
            receipt = f" ({change.receipt})" if change.receipt else ""
            lines.append(
                f"- successful change: {change.task}; files: {files}; checks: {checks}{receipt}"
            )
        return "\n".join(lines)


def _format_check(item: SuccessfulCheck) -> str:
    return item.command if item.cwd == "." else f"{item.cwd}/: {item.command}"
