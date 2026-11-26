from __future__ import annotations

import hashlib
import json
import re
import shlex
from dataclasses import asdict, dataclass
from typing import Literal

from codey.policies.command_line import ShellSpec


@dataclass(frozen=True)
class ShellWord:
    raw: str
    literal: str | None
    expansions: tuple[str, ...] = ()

    @classmethod
    def from_payload(cls, value: object) -> ShellWord:
        row = _object(value, {"raw", "literal", "expansions"})
        if not isinstance(row["raw"], str) or (row["literal"] is not None and not isinstance(row["literal"], str)):
            raise ValueError("invalid shell word")
        return cls(row["raw"], row["literal"], _strings(row["expansions"]))


@dataclass(frozen=True)
class ShellCommand:
    words: tuple[ShellWord, ...]

    @classmethod
    def from_payload(cls, value: object) -> ShellCommand:
        row = _object(value, {"words"})
        return cls(tuple(ShellWord.from_payload(word) for word in _sequence(row["words"])))


@dataclass(frozen=True)
class ShellRedirect:
    operator: str
    target: ShellWord | None

    @classmethod
    def from_payload(cls, value: object) -> ShellRedirect:
        row = _object(value, {"operator", "target"})
        if not isinstance(row["operator"], str):
            raise ValueError("invalid shell redirection")
        return cls(row["operator"], ShellWord.from_payload(row["target"]) if row["target"] is not None else None)


@dataclass(frozen=True)
class ShellAnalysis:
    command_sha256: str
    status: Literal["parsed", "opaque", "invalid"]
    parser: str
    commands: tuple[ShellCommand, ...] = ()
    operators: tuple[str, ...] = ()
    redirections: tuple[ShellRedirect, ...] = ()
    expansions: tuple[str, ...] = ()
    dynamic: bool = False
    reason: str = ""
    shell_sha256: str = ""

    @property
    def reusable(self) -> bool:
        return (self.status == "parsed" and not self.dynamic and not self.expansions and bool(self.commands)
                and all(word.literal is not None and not word.expansions for row in self.commands for word in row.words)
                and all(row.target is None or (row.target.literal is not None and not row.target.expansions)
                        for row in self.redirections))

    @property
    def simple_argv(self) -> tuple[str, ...] | None:
        if not self.reusable or len(self.commands) != 1 or self.operators or self.redirections:
            return None
        words = self.commands[0].words
        return tuple(word.literal for word in words if word.literal is not None) if words and all(
            word.literal is not None for word in words) else None

    def to_payload(self) -> dict[str, object]:
        return asdict(self)

    @classmethod
    def from_payload(cls, value: object) -> ShellAnalysis:
        row = _object(value, {"command_sha256", "status", "parser", "commands", "operators", "redirections",
                              "expansions", "dynamic", "reason", "shell_sha256"})
        if (not isinstance(row["status"], str) or row["status"] not in {"parsed", "opaque", "invalid"}
                or not isinstance(row["parser"], str) or not row["parser"] or type(row["dynamic"]) is not bool
                or not isinstance(row["reason"], str)):
            raise ValueError("invalid shell analysis metadata")
        status: Literal["parsed", "opaque", "invalid"] = (
            "parsed" if row["status"] == "parsed" else "opaque" if row["status"] == "opaque" else "invalid")
        return cls(_digest(row["command_sha256"]), status, row["parser"],
                   tuple(ShellCommand.from_payload(item) for item in _sequence(row["commands"])),
                   _strings(row["operators"]), tuple(ShellRedirect.from_payload(item) for item in _sequence(row["redirections"])),
                   _strings(row["expansions"]), row["dynamic"], row["reason"], _digest(row["shell_sha256"]))

    def matches(self, command: str, shell: ShellSpec) -> bool:
        return (self.command_sha256 == hashlib.sha256(command.encode("utf-8")).hexdigest()
                and self.shell_sha256 == shell_identity(shell))


def _object(value: object, keys: set[str]) -> dict[str, object]:
    if not isinstance(value, dict) or set(value) != keys:
        raise ValueError("shell analysis fields differ")
    return {str(key): item for key, item in value.items()}


def _digest(value: object) -> str:
    if not isinstance(value, str) or not re.fullmatch("[0-9a-f]{64}", value):
        raise ValueError("invalid shell analysis identity")
    return value


def _sequence(value: object) -> tuple[object, ...]:
    if not isinstance(value, (list, tuple)) or len(value) > 10000:
        raise ValueError("invalid shell analysis sequence")
    return tuple(value)


def _strings(value: object) -> tuple[str, ...]:
    values = _sequence(value)
    result: list[str] = []
    for item in values:
        if not isinstance(item, str):
            raise ValueError("invalid shell analysis strings")
        result.append(item)
    return tuple(result)


def shell_identity(shell: ShellSpec) -> str:
    return hashlib.sha256(json.dumps(shell.to_payload(), sort_keys=True).encode("utf-8")).hexdigest()


def parser_identity(shell: ShellSpec) -> str:
    if shell.kind == "cmd":
        return "cmd-literal/v1"
    if shell.kind == "powershell":
        return "PowerShell AST/" + (shell.version or "unknown")
    return "tree-sitter-bash/0.25.1"


_DYNAMIC_NAMES = {"eval", "source", ".", "exec", "command", "builtin", "env", "call", "start",
                  "invoke-expression", "iex", "invoke-command", "icm", "cmd", "powershell", "pwsh",
                  "bash", "zsh", "sh"}


def dynamic_name(words: tuple[ShellWord, ...]) -> bool:
    if not words or words[0].literal is None:
        return True
    name = words[0].literal.replace("\\", "/").rsplit("/", 1)[-1].lower().removesuffix(".exe")
    return name in _DYNAMIC_NAMES


def posix_literal(raw: str) -> str | None:
    try:
        words = shlex.split(raw, posix=True)
        return words[0] if len(words) == 1 else None
    except ValueError:
        return None
