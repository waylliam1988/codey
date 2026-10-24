"""Host shell contract and the tokenizer for direct-argv run commands.

Shell scripts are passed intact to their declared interpreter. The tokenizer
below is for direct execution and conservative command classification, not a
parser for arbitrary shell programs:

- POSIX platforms tokenize with ``shlex.split(..., posix=True)``;
- Windows tokenizes with ``posix=False`` (backslashes in ``C:\\path\\file.py``
  are path characters, not escapes) and then strips one layer of matching
  quotes per token;
- any other platform uses the POSIX rule.

Tokenization failure raises :class:`ValueError`; callers must fail closed
(deny, error outcome) instead of guessing a token boundary.
"""

from __future__ import annotations

import os
import re
import shlex
import subprocess
import sys
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import PureWindowsPath
from typing import Literal

COMMAND_WORKDIR_DESCRIPTION = (
    "Use a project-relative directory for path, or omit path to use the project root. "
    "Absolute paths and script filenames are not valid working directories."
)


@dataclass(frozen=True)
class ShellSpec:
    """One immutable interpreter contract, carried by the existing approval."""

    kind: Literal["cmd", "posix"]
    executable: str

    def __post_init__(self) -> None:
        if any(char in self.executable for char in ('"', '\0', '\r', '\n')):
            raise ValueError("invalid shell executable path")
        if self.kind == "cmd":
            path = PureWindowsPath(self.executable)
            if not path.is_absolute() or path.name.lower() != "cmd.exe":
                raise ValueError("Windows shell must be an absolute cmd.exe path")
        elif self.kind != "posix" or self.executable != "/bin/sh":
            raise ValueError("unsupported shell contract")

    def to_payload(self) -> dict[str, str]:
        return {"kind": self.kind, "executable": self.executable}

    @classmethod
    def from_payload(cls, value: object) -> ShellSpec:
        if not isinstance(value, dict) or set(value) != {"kind", "executable"}:
            raise ValueError("shell contract required; request a new command approval")
        kind, executable = value["kind"], value["executable"]
        if not isinstance(kind, str) or kind not in {"cmd", "posix"} or not isinstance(executable, str):
            raise ValueError("invalid shell contract")
        return cls("cmd" if kind == "cmd" else "posix", executable)

    def invocation(self, command: str) -> str | list[str]:
        if not isinstance(command, str) or not command.strip() or "\0" in command:
            raise ValueError("nonempty shell command without NUL required")
        if self.kind == "cmd":
            # CMD consumes a raw command tail. list2cmdline on the entire argv
            # would apply CRT escaping to the script's embedded quotes.
            return f'{subprocess.list2cmdline([self.executable])} /d /s /c "{command}"'
        return [self.executable, "-c", command]

    def guidance(self) -> str:
        common = (
            f"Shell command interpreter: {self.executable} (dialect: {self.kind}). "
            "Use syntax for this interpreter. Do not infer the shell from the user's terminal or workspace path. "
            "Use the tool's working directory instead of chaining cd. "
            "Prefer file tools for multiline scripts, then invoke the saved script; do not build nested quoted programs. "
        )
        if self.kind == "cmd":
            return common + (
                "Windows commands use cmd.exe syntax, not Bash heredocs (<<EOF) or implicit PowerShell syntax. "
                "Use double quotes for paths with spaces; single quotes do not group CMD arguments. "
                "Environment variables use %NAME%, not $NAME or $env:NAME. "
                "Use python -c for short Python snippets; invoke PowerShell explicitly with "
                "powershell -NoProfile -NonInteractive -Command when PowerShell syntax is needed. "
                "Do not assume POSIX utilities such as head, tail or sed exist. "
                "For run output, use output_tail_lines instead of a pipe to tail."
            )
        return common + (
            "Use POSIX /bin/sh syntax and $NAME environment variables. Bash-only arrays, [[ ... ]] and "
            "process substitution require an explicit Bash invocation. Quote paths with spaces. "
            "For run output, use output_tail_lines instead of a pipe to tail."
        )

    def compatibility_error(self, command: str) -> str:
        if (self.kind == "cmd" and _has_unquoted_heredoc(command)
                and not re.match(r'^\s*(?:"[^"]*bash(?:\.exe)?"|\S*bash(?:\.exe)?)\s+-', command)):
            return ("Bash heredoc is not supported by the Windows command shell; "
                    "use an explicit Bash command or a Windows-compatible command.")
        return ""


def resolve_shell(*, platform: str | None = None, environ: Mapping[str, str] | None = None) -> ShellSpec:
    """Resolve once at task admission; never switch dialect after a failure."""
    host = sys.platform if platform is None else platform
    env = os.environ if environ is None else environ
    if host == "win32":
        executable = env.get("COMSPEC") or str(PureWindowsPath(env.get("SystemRoot", r"C:\Windows"), "System32", "cmd.exe"))
        return ShellSpec("cmd", executable)
    return ShellSpec("posix", "/bin/sh")


def _has_unquoted_heredoc(command: str) -> bool:
    quoted = False
    escaped = False
    for index, char in enumerate(command):
        if escaped:
            escaped = False
        elif char == "^":
            escaped = True
        elif char == '"':
            quoted = not quoted
        elif not quoted and command[index:index + 2] == "<<":
            return True
    return False


def _is_windows(platform: str) -> bool:
    return platform.startswith("win32") or platform.startswith("cygwin")


def split_run_command(
    command: str,
    platform: str | None = None,
) -> list[str]:
    """Split a run/shell command into argv with one shared rule set."""

    text = (command or "")
    posix = not _is_windows(sys.platform if platform is None else platform)
    argv = shlex.split(text, posix=posix)
    if posix:
        return argv
    return [_strip_matching_quotes(token) for token in argv]


def _strip_matching_quotes(token: str) -> str:
    if len(token) >= 2 and token[0] == token[-1] and token[0] in {'"', "'"}:
        return token[1:-1]
    return token


__all__ = [
    "split_run_command",
    "ShellSpec",
    "resolve_shell",
    "COMMAND_WORKDIR_DESCRIPTION",
]
