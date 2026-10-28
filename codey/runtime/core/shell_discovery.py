"""Host discovery and binary observations; never choose another shell after admission."""
from __future__ import annotations

import base64
import hashlib
import os
import re
import shutil
from collections.abc import Mapping
from functools import lru_cache
from pathlib import Path, PureWindowsPath
from typing import Literal

from codey.env_names import SHELL_EXECUTABLE_ENV
from codey.runtime.core import cancellation

ShellKind = Literal["cmd", "powershell", "bash", "zsh", "sh"]
_NAMES: dict[str, ShellKind] = {"cmd": "cmd", "powershell": "powershell", "pwsh": "powershell",
                               "bash": "bash", "zsh": "zsh", "sh": "sh", "dash": "sh"}


def discover_shell(
    platform: str,
    env: Mapping[str, str],
    *,
    startup: Literal["noninteractive", "login"] | None = None,
) -> tuple[ShellKind, str, str | None, str]:
    selected = env.get(SHELL_EXECUTABLE_ENV, "").strip()
    if not selected:
        selected = ((env.get("COMSPEC") or str(PureWindowsPath(env.get("SystemRoot", r"C:\Windows"), "System32", "cmd.exe")))
                    if platform == "win32" else "/bin/sh")
    name = PureWindowsPath(selected).stem.lower() if platform == "win32" else Path(selected).name
    kind = _NAMES.get(name)
    if kind is None:
        raise ValueError(f"unsupported shell executable: {name}")
    if kind == "cmd" and startup == "login":
        raise ValueError("unsupported shell startup mode: cmd does not support login")
    candidate = selected if os.path.isabs(selected) else shutil.which(selected, path=env.get("PATH"))
    if candidate is None and platform == "win32" and name in {"bash", "sh"}:
        git = shutil.which("git", path=env.get("PATH"))
        candidate = str(Path(git).parent.parent / "bin" / f"{name}.exe") if git else None
    if not candidate or not Path(candidate).is_file():
        raise ValueError(f"shell executable unavailable: {selected}")
    # abspath preserves /bin/sh's argv[0] semantics when it is a symlink to Bash.
    executable = os.path.abspath(candidate)
    stat = Path(executable).stat()
    version, fingerprint = _observe(executable, kind, stat.st_size, stat.st_mtime_ns)
    return kind, executable, version, fingerprint


@lru_cache(maxsize=32)
def _observe(executable: str, kind: ShellKind, size: int, mtime_ns: int) -> tuple[str | None, str]:
    del size, mtime_ns  # Cache key identifies the currently observed file.
    fingerprint = binary_fingerprint(executable)
    if kind == "cmd":
        args = [executable, "/d", "/c", "ver"]
    elif kind == "powershell":
        script = "$PSVersionTable.PSVersion.ToString()"
        args = [executable, "-NoLogo", "-NoProfile", "-NonInteractive", "-EncodedCommand",
                base64.b64encode(script.encode("utf-16-le")).decode("ascii")]
    elif kind == "sh":
        args = [executable, "-c", 'printf "%s" "${BASH_VERSION:-}${ZSH_VERSION:-}${KSH_VERSION:-}"']
    else:
        args = [executable, "--version"]
    result = cancellation.run_process(args, cwd=Path(executable).parent, timeout=5, capture_limit_bytes=16384)
    if result.returncode != 0:
        raise ValueError(f"shell version probe failed: {executable}")
    match = re.search(r"\d+(?:\.\d+)+(?:[^\s\]]*)?", result.stdout)
    return (match.group(0) if match else None), fingerprint


def binary_fingerprint(executable: str) -> str:
    with Path(executable).open("rb") as source:
        return hashlib.file_digest(source, "sha256").hexdigest()
