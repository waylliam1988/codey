"""Bounded project facts learned only from successful local checks."""

from __future__ import annotations

import re
import shlex
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from codey.local_store import (
    DEFAULT_STATE_HOME,
    project_key,
    read_json,
    write_json_atomic,
)


SCHEMA_VERSION = 1
MAX_VERIFIED_COMMANDS = 8
MAX_COMMAND_CHARS = 300
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
class ProjectFacts:
    commands: tuple[VerifiedCommand, ...] = ()


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
        argv = shlex.split(text)
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
        return ProjectFacts(tuple(commands[-MAX_VERIFIED_COMMANDS:]))

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
        return "\n".join(lines)
