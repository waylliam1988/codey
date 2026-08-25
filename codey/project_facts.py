"""Bounded project facts learned only from successful local checks."""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Sequence

from codey.command_line import split_run_command
from codey.local_store import (
    DEFAULT_STATE_HOME,
    project_key,
    read_json,
    write_json_atomic,
)


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

    def to_payload(self) -> dict:
        payload = {"command": self.command, "cwd": self.cwd, "kind": self.kind}
        if self.entry_file:
            payload["entry_file"] = self.entry_file
        return payload


@dataclass(frozen=True)
class SuccessfulCheck:
    command: str
    cwd: str = "."

    def to_payload(self) -> dict:
        return {"command": self.command, "cwd": self.cwd}


@dataclass(frozen=True)
class SuccessfulChange:
    task: str
    files: tuple[str, ...] = ()
    checks: tuple[SuccessfulCheck, ...] = ()
    receipt: str = ""

    def to_payload(self) -> dict:
        payload = {
            "task": self.task,
            "files": list(self.files),
            "checks": [item.to_payload() for item in self.checks],
        }
        if self.receipt:
            payload["receipt"] = self.receipt
        return payload


@dataclass(frozen=True)
class ProjectFacts:
    commands: tuple[VerifiedCommand, ...] = ()
    successful_changes: tuple[SuccessfulChange, ...] = ()


def _safe_cwd(value: str) -> str | None:
    text = str(value or ".").strip().replace("\\", "/") or "."
    path = PurePosixPath(text)
    if path.is_absolute() or ".." in path.parts:
        return None
    return path.as_posix()


def _verified_command(command: str, cwd: str) -> VerifiedCommand | None:
    text = str(command or "").strip()
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
    path = PurePosixPath(text)
    if path.is_absolute() or ".." in path.parts:
        return None
    return path.as_posix()


def _safe_receipt(value: object) -> str:
    text = str(value or "").replace("\r\n", "\n").replace("\r", "\n").strip()
    text = " ".join(line.strip() for line in text.splitlines() if line.strip())
    return text[:240].rstrip()


def _successful_check_from_object(value: object) -> SuccessfulCheck | None:
    if isinstance(value, dict):
        command = value.get("command", "")
        cwd = value.get("cwd", ".")
    elif isinstance(value, str):
        command = value
        cwd = "."
    else:
        command = getattr(value, "command", "")
        cwd = getattr(value, "cwd", ".")
    fact = _verified_command(str(command or ""), str(cwd or "."))
    if fact is None or fact.kind != "check":
        return None
    return SuccessfulCheck(fact.command, fact.cwd)


def _successful_change(
    *,
    task: object,
    files: Sequence[object],
    checks: Sequence[object],
    receipt: object = "",
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
    )


def _successful_checks_from_payload(value: object) -> tuple[SuccessfulCheck, ...]:
    if not isinstance(value, (list, tuple)):
        return ()
    return tuple(
        item
        for item in (_successful_check_from_object(check) for check in value)
        if item is not None
    )


def _successful_change_from_payload(value: object) -> SuccessfulChange | None:
    if not isinstance(value, dict):
        return None
    return _successful_change(
        task=value.get("task"),
        files=value.get("files") if isinstance(value.get("files"), (list, tuple)) else (),
        checks=_successful_checks_from_payload(value.get("checks")),
        receipt=value.get("receipt", ""),
    )


class ProjectFactsStore:
    """Keep one small, overwritable set of verified facts per project."""

    def __init__(self, state_home: str | Path = DEFAULT_STATE_HOME) -> None:
        self.state_home = Path(state_home)

    def path_for(self, project: str | Path) -> Path:
        return self.state_home / "projects" / project_key(project) / "facts.json"

    def load(self, project: str | Path) -> ProjectFacts:
        payload = read_json(self.path_for(project))
        if not payload or payload.get("schema_version") != SCHEMA_VERSION:
            return ProjectFacts()
        commands: list[VerifiedCommand] = []
        for item in payload.get("commands") or []:
            if not isinstance(item, dict):
                continue
            fact = _verified_command(item.get("command", ""), item.get("cwd", "."))
            if fact is None:
                continue
            commands.append(fact)
        successful_changes: list[SuccessfulChange] = []
        for item in payload.get("successful_changes") or []:
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
        facts = self.load(project)
        commands = [
            item
            for item in facts.commands
            if (item.command, item.cwd) != (fact.command, fact.cwd)
        ]
        commands.append(fact)
        commands = commands[-MAX_VERIFIED_COMMANDS:]
        write_json_atomic(
            self.path_for(project),
            {
                "schema_version": SCHEMA_VERSION,
                "commands": [item.to_payload() for item in commands],
                "successful_changes": [
                    item.to_payload()
                    for item in facts.successful_changes[-MAX_SUCCESSFUL_CHANGES:]
                ],
            },
        )
        return True

    def record_successful_change(
        self,
        project: str | Path,
        *,
        task: object,
        files: Sequence[object],
        checks: Sequence[object] | None = None,
        check_commands: Sequence[object] | None = None,
        receipt: object = "",
    ) -> bool:
        raw_checks = checks if checks is not None else check_commands
        change = _successful_change(
            task=task,
            files=files,
            checks=raw_checks or (),
            receipt=receipt,
        )
        if change is None:
            return False
        facts = self.load(project)
        changes = [
            item
            for item in facts.successful_changes
            if (item.task, item.files, item.checks) != (change.task, change.files, change.checks)
        ]
        changes.append(change)
        changes = changes[-MAX_SUCCESSFUL_CHANGES:]
        write_json_atomic(
            self.path_for(project),
            {
                "schema_version": SCHEMA_VERSION,
                "commands": [item.to_payload() for item in facts.commands[-MAX_VERIFIED_COMMANDS:]],
                "successful_changes": [item.to_payload() for item in changes],
            },
        )
        return True

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
