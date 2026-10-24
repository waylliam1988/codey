"""One bounded analysis contract; dialect parsers never execute the input script."""
from __future__ import annotations

import hashlib
import json
import re
import shlex
from collections.abc import Mapping
from dataclasses import asdict, dataclass, replace
from typing import Literal, cast

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
    return cast(dict[str, object], value)


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
    if any(not isinstance(item, str) for item in values):
        raise ValueError("invalid shell analysis strings")
    return cast(tuple[str, ...], values)


def shell_identity(shell: ShellSpec) -> str:
    return hashlib.sha256(json.dumps(shell.to_payload(), sort_keys=True).encode("utf-8")).hexdigest()


def parser_identity(shell: ShellSpec) -> str:
    if shell.kind == "cmd":
        return "cmd-literal/v1"
    if shell.kind == "powershell":
        return "PowerShell AST/" + (shell.version or "unknown")
    return "tree-sitter-bash/0.25.1"


def bound_analysis(command: str, shell: ShellSpec, value: object) -> ShellAnalysis:
    analysis = ShellAnalysis.from_payload(value)
    if not analysis.matches(command, shell) or analysis.parser != parser_identity(shell):
        raise ValueError("shell analysis differs from command, interpreter or parser; request a new approval")
    return analysis


def request_analysis(request: Mapping[str, object]) -> tuple[ShellSpec, ShellAnalysis]:
    command = request.get("command")
    if not isinstance(command, str):
        raise ValueError("approval command required")
    shell = ShellSpec.from_payload(request.get("shell"))
    return shell, bound_analysis(command, shell, request.get("analysis"))


_DYNAMIC_NAMES = {"eval", "source", ".", "exec", "command", "builtin", "env", "call", "start",
                  "invoke-expression", "iex", "invoke-command", "icm", "cmd", "powershell", "pwsh",
                  "bash", "zsh", "sh"}


def dynamic_name(words: tuple[ShellWord, ...]) -> bool:
    if not words or words[0].literal is None:
        return True
    name = words[0].literal.replace("\\", "/").rsplit("/", 1)[-1].lower().removesuffix(".exe")
    return name in _DYNAMIC_NAMES


def analyze_shell(command: str, shell: ShellSpec) -> ShellAnalysis:
    digest = hashlib.sha256(command.encode("utf-8")).hexdigest()
    if not command.strip() or "\0" in command:
        result = ShellAnalysis(digest, "invalid", shell.kind, reason="nonempty shell command without NUL required")
    elif len(command.encode("utf-8")) > 32768:
        result = ShellAnalysis(digest, "opaque", shell.kind, reason="shell analysis byte budget exceeded")
    elif shell.kind == "cmd":
        result = _cmd(command, digest)
    elif shell.kind == "powershell":
        from codey.policies.shell_powershell import analyze_powershell
        result = analyze_powershell(command, shell, digest)
    else:
        from codey.policies.shell_posix import analyze_posix
        result = analyze_posix(command, shell, digest)
    return replace(result, shell_sha256=shell_identity(shell), parser=parser_identity(shell))


def _cmd(command: str, digest: str) -> ShellAnalysis:
    words: list[ShellWord] = []
    commands: list[ShellCommand] = []
    operators: list[str] = []
    redirects: list[ShellRedirect] = []
    expansions: list[str] = []
    reason = ""
    raw, literal, quoted, index = "", "", False, 0
    target: str | None = None

    def finish() -> None:
        nonlocal raw, literal, target
        if not raw:
            return
        word = _cmd_word(raw, literal)
        expansions.extend(word.expansions)
        if target is not None:
            redirects.append(ShellRedirect(target, word))
            target = None
        else:
            words.append(word)
        raw, literal = "", ""

    while index < len(command):
        char = command[index]
        if char == "^":
            reason = "CMD caret expansion requires explicit approval"
            raw += char
            literal += char
        elif char == '"':
            quoted = not quoted
            raw += char
        elif not quoted and char in "()":
            reason = "CMD grouping/control syntax requires explicit approval"
            raw += char
            literal += char
        elif not quoted and char in "&|\r\n":
            finish()
            operator = char
            if char in "&|" and index + 1 < len(command) and command[index + 1] == char:
                operator += char
                index += 1
            if words:
                commands.append(ShellCommand(tuple(words)))
                words = []
            operators.append(operator)
        elif not quoted and char in "<>":
            descriptor = ""
            if raw.isdecimal():
                descriptor, raw, literal = raw, "", ""
            finish()
            operator = descriptor + char
            if index + 1 < len(command) and command[index + 1] == char:
                if char == "<":
                    return ShellAnalysis(digest, "invalid", "cmd-literal/v1", reason="Bash heredoc is not CMD syntax")
                operator += char
                index += 1
            if index + 1 < len(command) and command[index + 1] == "&":
                operator += "&"
                index += 1
            target = operator
        elif not quoted and char.isspace():
            finish()
        else:
            raw += char
            literal += char
        index += 1
    finish()
    if words:
        commands.append(ShellCommand(tuple(words)))
    status, reason = _cmd_status(commands, quoted, target, reason)
    return ShellAnalysis(digest, status, "cmd-literal/v1", tuple(commands), tuple(operators),
                         tuple(redirects), tuple(expansions), any(dynamic_name(row.words) for row in commands), reason)


def _cmd_status(commands: list[ShellCommand], quoted: bool, target: str | None,
                reason: str) -> tuple[Literal["parsed", "opaque", "invalid"], str]:
    if target is not None:
        return "invalid", "missing CMD redirection target"
    if quoted:
        reason = "CMD unmatched quote requires explicit approval"
    if any(word.raw.startswith("'") for row in commands for word in row.words):
        reason = "CMD single quotes do not group arguments"
    if any(row.words and (row.words[0].literal or "").lstrip("@").lower() in
           {"for", "if", "goto", "setlocal", "endlocal"} for row in commands):
        reason = "CMD control flow requires explicit approval"
    return "opaque" if reason else "parsed", reason


def _cmd_word(raw: str, literal: str) -> ShellWord:
    expanded = tuple(re.findall(r"%[^%\r\n]+%|![^!\r\n]+!|%[0-9*]", raw))
    return ShellWord(raw, None if expanded else literal, expanded)


def posix_literal(raw: str) -> str | None:
    try:
        words = shlex.split(raw, posix=True)
        return words[0] if len(words) == 1 else None
    except ValueError:
        return None
