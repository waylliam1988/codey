"""The published runtime contract, independent of a repository checkout."""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from codey.control.protocol import ControlError
from codey.storage.atomic_io import filesystem_path

DESCRIPTOR = "codey/runtime.json"


def package_files(root: Path) -> list[str]:
    """One inventory policy for source publication, wheels and runtime checks."""
    root = filesystem_path(root)
    files: list[str] = []
    package = root / "codey"
    if package.is_symlink():
        raise ControlError("INVALID_ARGUMENT", "Runtime cannot contain symbolic links")
    for directory, dirs, names in os.walk(package, followlinks=False):
        for name in [*dirs, *names]:
            if Path(directory, name).is_symlink():
                raise ControlError("INVALID_ARGUMENT", "Runtime cannot contain symbolic links")
        dirs[:] = sorted(name for name in dirs if name != "__pycache__")
        files.extend(Path(directory, name).relative_to(root).as_posix() for name in names
                     if not name.endswith((".pyc", ".log")) and Path(directory, name).relative_to(root).as_posix() != DESCRIPTOR)
    return sorted(files)


def read_runtime(root: Path) -> dict[str, Any]:
    try:
        data = json.loads((root / DESCRIPTOR).read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            raise ValueError("Invalid runtime descriptor")
        valid = (data.get("schema") == 1
                 and isinstance(data.get("version"), str) and bool(data["version"])
                 and isinstance(data.get("requires_python"), str)
                 and all(isinstance(data.get(key), list) and all(isinstance(item, str) for item in data[key])
                         for key in ("dependencies", "mcp_dependencies", "files")))
        if not valid or not data["files"] or len(data["files"]) > 10_000:
            raise ValueError("Invalid runtime descriptor")
        if data["files"] != package_files(root):
            raise ValueError("Runtime inventory differs from its published descriptor")
        if not {"codey/__main__.py", "codey/web/index.html"}.issubset(data["files"]):
            raise ValueError("Runtime requires its entry point and built frontend")
        return data
    except (OSError, ValueError, TypeError) as exc:
        raise ControlError("RUNTIME_NOT_FOUND", "Published Codey runtime is missing or incomplete") from exc


def runtime_files(root: Path) -> list[Path]:
    data = read_runtime(root)
    return [root / name for name in sorted([DESCRIPTOR, *data["files"]])]


def dependencies(root: Path) -> object:
    data = read_runtime(root)
    return [data["requires_python"], data["dependencies"], data["mcp_dependencies"]]
