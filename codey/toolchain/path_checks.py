"""Shared workspace path checks and version-checked atomic writes."""

from __future__ import annotations

from pathlib import Path

from codey.storage.atomic_io import write_bytes_atomic, write_text_atomic
from codey.toolchain.constants import STALE_READ_REFERENCE_CODE, STALE_READ_REFERENCE_ERROR
from codey.toolchain.outcome import ToolOutcome
from codey.workspace.paths import safe_join


def _symlink_path_error(root: Path, rel: str, *, tool: str) -> ToolOutcome | None:
    reason = _raw_path_symlink_reason(root, rel, tool=tool)
    if reason:
        return ToolOutcome.error(reason, error_code="symlink_path")
    return None


def _checked_tool_path(
    root: Path,
    rel: str,
    *,
    tool: str,
) -> tuple[Path | None, ToolOutcome | None]:
    try:
        path = safe_join(root, rel)
    except (OSError, RuntimeError, ValueError) as exc:
        return None, ToolOutcome.error(str(exc), error_code="workspace_escape")
    symlink_error = _symlink_path_error(root, rel, tool=tool)
    if symlink_error is not None:
        return None, symlink_error
    return path, None


def _verify_file_for_use(
    root: Path,
    rel: str,
    path: Path,
    *,
    tool: str,
) -> ToolOutcome | None:
    """Final fail-closed file check immediately before use.

    Re-verifies the symlink walk (narrows the check-use gap) and maps any
    ``lstat``/stat failure to a structured denial. File tools must never
    let a bare ``OSError``/``ValueError`` escape to the caller.
    """

    symlink_error = _symlink_path_error(root, rel, tool=tool)
    if symlink_error is not None:
        return symlink_error
    try:
        if path.is_symlink():
            return ToolOutcome.error(
                f"symlink paths are not supported for {tool}: {rel}",
                error_code="symlink_path",
            )
        if not path.is_file():
            return ToolOutcome.error(f"not a file: {rel}")
    except (OSError, ValueError):
        return ToolOutcome.error(
            f"path check failed for {tool}: {rel}",
            error_code="symlink_path",
        )
    return None


def _verify_dir_for_use(
    root: Path,
    rel: str,
    path: Path,
    *,
    tool: str,
) -> ToolOutcome | None:
    """Final fail-closed directory check immediately before use."""

    symlink_error = _symlink_path_error(root, rel, tool=tool)
    if symlink_error is not None:
        return symlink_error
    try:
        if path.is_symlink():
            return ToolOutcome.error(
                f"symlink paths are not supported for {tool}: {rel}",
                error_code="symlink_path",
            )
        if not path.is_dir():
            return ToolOutcome.error(f"not a directory: {rel}")
    except (OSError, ValueError):
        return ToolOutcome.error(
            f"path check failed for {tool}: {rel}",
            error_code="symlink_path",
        )
    return None


def _verify_path_exists(
    root: Path,
    rel: str,
    path: Path,
    *,
    tool: str,
) -> ToolOutcome | None:
    """Final fail-closed existence check immediately before scanning."""

    symlink_error = _symlink_path_error(root, rel, tool=tool)
    if symlink_error is not None:
        return symlink_error
    try:
        if path.is_symlink():
            return ToolOutcome.error(
                f"symlink paths are not supported for {tool}: {rel}",
                error_code="symlink_path",
            )
        if not path.exists():
            return ToolOutcome.error(f"path not found: {rel}")
    except (OSError, ValueError):
        return ToolOutcome.error(
            f"path check failed for {tool}: {rel}",
            error_code="symlink_path",
        )
    return None


def _write_text_after_final_path_check(
    root: Path,
    rel: str,
    content: str,
    *,
    tool: str,
    expected_version: str | None = None,
) -> ToolOutcome | None:
    path, error = _checked_tool_path(root, rel, tool=tool)
    if error is not None or path is None:
        return error or ToolOutcome.error("path could not be resolved")
    if expected_version is not None:
        from codey.workspace.paths import path_hash

        preserve_newlines = tool == "edit" or expected_version.startswith("raw:")
        if path_hash(path, preserve_newlines=preserve_newlines) != expected_version.removeprefix("raw:"):
            return ToolOutcome.error(STALE_READ_REFERENCE_ERROR, error_code=STALE_READ_REFERENCE_CODE)
    if tool == "edit":
        # Edit coordinates already preserve source endings; text-mode encoding
        # would normalize untouched mixed-ending lines a second time.
        write_bytes_atomic(path, content.encode("utf-8"))
    else:
        write_text_atomic(path, content)
    return None


def _raw_path_symlink_reason(root: Path, rel: str, *, tool: str) -> str:
    raw = Path(rel or ".")
    try:
        parts = raw.relative_to(root).parts if raw.is_absolute() else raw.parts
    except ValueError:
        return ""
    current = root
    for part in parts:
        if part in {"", "."}:
            continue
        current = current / part
        try:
            if current.is_symlink():
                return f"symlink paths are not supported for {tool}: {rel}"
        except OSError:
            # Fail closed: an unverifiable path component must deny,
            # never silently allow a possible symlink escape.
            return f"symlink check failed for {tool}: {rel}"
    return ""
