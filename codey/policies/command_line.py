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

import base64
import os
import re
import shlex
import subprocess
import sys
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import PureWindowsPath
from typing import Literal

from codey.env_names import SHELL_STARTUP_ENV

COMMAND_WORKDIR_DESCRIPTION = (
    "Use a project-relative directory for path, or omit path to use the project root. "
    "Absolute paths and script filenames are not valid working directories."
)


@dataclass(frozen=True)
class ShellSpec:
    """One immutable interpreter contract, carried by the existing approval."""

    kind: Literal["cmd", "powershell", "bash", "zsh", "sh"]
    executable: str
    version: str | None
    startup: Literal["noninteractive", "login"]
    binary_sha256: str

    def __post_init__(self) -> None:
        if any(char in self.executable for char in ('"', '\0', '\r', '\n')):
            raise ValueError("invalid shell executable path")
        if self.kind not in {"cmd", "powershell", "bash", "zsh", "sh"}:
            raise ValueError("unsupported shell contract")
        if self.startup not in {"noninteractive", "login"} or (self.kind == "cmd" and self.startup == "login"):
            raise ValueError("unsupported shell startup mode")
        if self.version is not None and (not isinstance(self.version, str) or not self.version.strip()):
            raise ValueError("invalid shell version")
        if not re.fullmatch(r"[a-f0-9]{64}", self.binary_sha256):
            raise ValueError("shell binary fingerprint required")
        if PureWindowsPath(self.executable).is_absolute():
            path = PureWindowsPath(self.executable)
            name = path.stem.lower()
        elif self.executable.startswith("/"):
            name = self.executable.rsplit("/", 1)[-1]
        else:
            raise ValueError("shell executable must be an absolute path")
        names = {"cmd": {"cmd"}, "powershell": {"pwsh", "powershell"}, "bash": {"bash"},
                 "zsh": {"zsh"}, "sh": {"sh", "dash"}}
        if name not in names[self.kind]:
            raise ValueError("shell kind differs from executable")

    def to_payload(self) -> dict[str, object]:
        return {"kind": self.kind, "executable": self.executable, "version": self.version,
                "startup": self.startup, "binary_sha256": self.binary_sha256}

    @classmethod
    def from_payload(cls, value: object) -> ShellSpec:
        if not isinstance(value, dict) or set(value) != {"kind", "executable", "version", "startup", "binary_sha256"}:
            raise ValueError("shell contract required; request a new command approval")
        kind, executable = value["kind"], value["executable"]
        if (not isinstance(kind, str) or not isinstance(executable, str)
                or not isinstance(value["startup"], str) or not isinstance(value["binary_sha256"], str)
                or (value["version"] is not None and not isinstance(value["version"], str))):
            raise ValueError("invalid shell contract")
        from typing import cast
        return cls(cast(Literal["cmd", "powershell", "bash", "zsh", "sh"], kind), executable,
                   value["version"], cast(Literal["noninteractive", "login"], value["startup"]), value["binary_sha256"])

    def invocation(self, command: str) -> str | list[str]:
        if not isinstance(command, str) or not command.strip() or "\0" in command:
            raise ValueError("nonempty shell command without NUL required")
        if self.kind == "cmd":
            # CMD consumes a raw command tail. list2cmdline on the entire argv
            # would apply CRT escaping to the script's embedded quotes.
            return f'{subprocess.list2cmdline([self.executable])} /d /s /c "{command}"'
        if self.kind == "powershell":
            args = [self.executable, "-NoLogo", "-NonInteractive"]
            if self.startup == "noninteractive":
                args.append("-NoProfile")
            # Encoding only transports the script; do not change its exit semantics.
            encoding = "$OutputEncoding=[Console]::OutputEncoding=[System.Text.UTF8Encoding]::new($false);\n"
            return [*args, "-EncodedCommand", base64.b64encode((encoding + command).encode("utf-16-le")).decode("ascii")]
        if self.startup == "login":
            return [self.executable, "-lc", command]
        flags = {"bash": ["--noprofile", "--norc"], "zsh": ["-f"], "sh": []}
        return [self.executable, *flags[self.kind], "-c", command]

    def guidance(self) -> str:
        common = (
            f"Shell command interpreter: {self.executable} (dialect: {self.kind}, "
            f"version: {self.version or 'not exposed'}, startup: {self.startup}). "
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
        if self.kind == "powershell":
            return common + (
                "Use PowerShell syntax and $env:NAME environment variables. Pipelines can carry objects. "
                "Use -LiteralPath for literal file paths. POSIX heredocs are not PowerShell syntax. "
                "For a native program's exact exit code, end with exit $LASTEXITCODE."
            )
        dialect = ("Use POSIX sh syntax; Bash arrays, [[ ... ]] and process substitution require explicit Bash. "
                   if self.kind == "sh" else f"Use {self.kind} syntax. ")
        return common + dialect + "Environment variables use $NAME. Quote paths with spaces."

def resolve_shell(*, platform: str | None = None, environ: Mapping[str, str] | None = None) -> ShellSpec:
    """Resolve once at task admission; never switch dialect after a failure."""
    host = sys.platform if platform is None else platform
    env = os.environ if environ is None else environ
    mode = env.get(SHELL_STARTUP_ENV, "noninteractive")
    if mode not in {"noninteractive", "login"}:
        raise ValueError("shell startup must be noninteractive or login")
    from codey.runtime.core.shell_discovery import discover_shell
    kind, executable, version, fingerprint = discover_shell(host, env)
    if kind == "cmd" and mode == "login":
        raise ValueError("unsupported shell startup mode: cmd does not support login")
    return ShellSpec(kind, executable, version, "login" if mode == "login" else "noninteractive", fingerprint)


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
