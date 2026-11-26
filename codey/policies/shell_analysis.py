"""One bounded analysis contract; dialect parsers never execute the input script."""
from __future__ import annotations

import hashlib
import re
from collections.abc import Mapping
from dataclasses import replace
from typing import Literal

from codey.policies.command_line import ShellSpec
from codey.policies.shell_model import _DYNAMIC_NAMES as _DYNAMIC_NAMES
from codey.policies.shell_model import ShellAnalysis as ShellAnalysis
from codey.policies.shell_model import ShellCommand as ShellCommand
from codey.policies.shell_model import ShellRedirect as ShellRedirect
from codey.policies.shell_model import ShellWord as ShellWord
from codey.policies.shell_model import _digest as _digest
from codey.policies.shell_model import _object as _object
from codey.policies.shell_model import _sequence as _sequence
from codey.policies.shell_model import _strings as _strings
from codey.policies.shell_model import dynamic_name as dynamic_name
from codey.policies.shell_model import parser_identity as parser_identity
from codey.policies.shell_model import posix_literal as posix_literal
from codey.policies.shell_model import shell_identity as shell_identity


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


def analyze_shell(command: str, shell: ShellSpec) -> ShellAnalysis:
    return _inspect_shell(command, shell, prepare_trailing_quote=False)[1]


def prepare_shell_command(command: str, shell: ShellSpec) -> tuple[str, ShellAnalysis]:
    """Prepare one approval command through the same bounded dialect analysis."""
    return _inspect_shell(command, shell, prepare_trailing_quote=True)


def _inspect_shell(command: str, shell: ShellSpec, *, prepare_trailing_quote: bool) -> tuple[str, ShellAnalysis]:
    digest = hashlib.sha256(command.encode("utf-8")).hexdigest()
    if not command.strip() or "\0" in command:
        result = ShellAnalysis(digest, "invalid", shell.kind, reason="nonempty shell command without NUL required")
    elif len(command.encode("utf-8")) > 32768:
        result = ShellAnalysis(digest, "opaque", shell.kind, reason="shell analysis byte budget exceeded")
    elif shell.kind == "cmd":
        result = _cmd(command, digest)
    elif shell.kind == "powershell":
        from codey.policies.shell_powershell import inspect_powershell
        command, result = inspect_powershell(command, shell, digest, prepare_trailing_quote=prepare_trailing_quote)
    else:
        from codey.policies.shell_posix import analyze_posix
        result = analyze_posix(command, shell, digest)
    return command, replace(result, shell_sha256=shell_identity(shell), parser=parser_identity(shell))


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
