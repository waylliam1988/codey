"""One tokenizer for every run/shell command decision path.

Policy analysis (approval risk, allow-lists) and actual execution must see the
same argv. This module is the single split point:

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
import shlex
import sys


def shell_executor() -> str:
    """Default interpreter used by subprocess shell=True on this host."""
    return os.environ.get("COMSPEC", "cmd.exe") if os.name == "nt" else "/bin/sh"


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
    "shell_executor",
]
