"""The published runtime contract, independent of a repository checkout."""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Literal, TypedDict

from codey.control.protocol import ControlError
from codey.storage.atomic_io import filesystem_path
from codey.types.json_value import require_json_string, require_json_strings
from codey.utils.object_fields import object_fields

DESCRIPTOR = "codey/runtime.json"


class RuntimeDescriptor(TypedDict):
    schema: Literal[1]
    version: str
    requires_python: str
    dependencies: list[str]
    mcp_dependencies: list[str]
    files: list[str]


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


def read_runtime(root: Path) -> RuntimeDescriptor:
    try:
        data: object = json.loads((root / DESCRIPTOR).read_text(encoding="utf-8"))
        if not object_fields(data) or type(data.get("schema")) is not int or data["schema"] != 1:
            raise ValueError("Invalid runtime descriptor")
        version = require_json_string(data.get("version"), "version")
        files = require_json_strings(data.get("files"), "files")
        if not version or not files or len(files) > 10_000:
            raise ValueError("Invalid runtime descriptor")
        if files != package_files(root):
            raise ValueError("Runtime inventory differs from its published descriptor")
        if not {"codey/__main__.py", "codey/web/index.html"}.issubset(files):
            raise ValueError("Runtime requires its entry point and built frontend")
        return RuntimeDescriptor(
            schema=1, version=version, files=files,
            requires_python=require_json_string(data.get("requires_python"), "requires_python"),
            dependencies=require_json_strings(data.get("dependencies"), "dependencies"),
            mcp_dependencies=require_json_strings(data.get("mcp_dependencies"), "mcp_dependencies"),
        )
    except (OSError, ValueError, TypeError) as exc:
        raise ControlError("RUNTIME_NOT_FOUND", "Published Codey runtime is missing or incomplete") from exc


def runtime_files(root: Path) -> list[Path]:
    data = read_runtime(root)
    return [root / name for name in sorted([DESCRIPTOR, *data["files"]])]


def dependencies(root: Path) -> object:
    data = read_runtime(root)
    return [data["requires_python"], data["dependencies"], data["mcp_dependencies"]]
