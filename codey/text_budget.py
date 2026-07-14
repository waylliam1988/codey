"""Small text-budget helpers shared by user-approved and controlled commands."""

from __future__ import annotations

import re
from pathlib import Path


OUTPUT_OMISSION_MARKER = "\n\n... middle of output omitted ...\n\n"
_PYTHON_FRAME_RE = re.compile(r'^\s*File "([^"]+)", line \d+(?:, in .*)?$')
_NODE_FRAME_RE = re.compile(r"^\s*at\s+")
_PYTHON_DEPENDENCY_SEGMENTS = {
    ".venv",
    "dist-packages",
    "site-packages",
    "venv",
}
_NODE_DEPENDENCY_SEGMENTS = {
    ".pnpm",
    "node_modules",
}
_NODE_FRAME_LOCATION_RE = re.compile(r":\d+:\d+(?:\)?\s*)?$")


def clip_middle(
    text: str,
    limit: int,
    marker: str = OUTPUT_OMISSION_MARKER,
) -> tuple[str, bool]:
    """Keep both ends of long text while respecting one total character limit."""
    if len(text) <= limit:
        return text, False
    if limit <= 0:
        return "", True
    if len(marker) >= limit:
        return marker[:limit], True
    available = limit - len(marker)
    head = (available + 1) // 2
    tail = available - head
    clipped = text[:head].rstrip() + marker
    if tail:
        clipped += text[-tail:].lstrip()
    return clipped, True


def prune_dependency_stack_frames(text: str, project_root: Path) -> str:
    """Fold obvious dependency stack frames while preserving user-code evidence."""
    lines = text.splitlines(keepends=True)
    if not lines:
        return text

    project_parts = _path_parts(str(project_root))
    rendered: list[str] = []
    omitted = 0
    omitted_eol = "\n"
    changed = False
    index = 0

    def flush_omitted() -> None:
        nonlocal omitted
        if omitted <= 0:
            return
        plural = "" if omitted == 1 else "s"
        rendered.append(
            f"[... {omitted} dependency stack frame{plural} omitted ...]"
            f"{omitted_eol}"
        )
        omitted = 0

    while index < len(lines):
        line = lines[index]
        python_path = _python_dependency_frame_path(line)
        if python_path and _has_dependency_segment(
            python_path,
            project_parts,
            _PYTHON_DEPENDENCY_SEGMENTS,
        ):
            changed = True
            omitted += 1
            omitted_eol = _line_ending(line) or omitted_eol
            index += 1
            if index < len(lines) and _is_python_frame_source_line(lines[index]):
                omitted_eol = _line_ending(lines[index]) or omitted_eol
                index += 1
            continue

        node_path = _node_dependency_frame_path(line)
        if node_path and _has_dependency_segment(
            node_path,
            project_parts,
            _NODE_DEPENDENCY_SEGMENTS,
        ):
            changed = True
            omitted += 1
            omitted_eol = _line_ending(line) or omitted_eol
            index += 1
            continue

        flush_omitted()
        rendered.append(line)
        index += 1

    flush_omitted()
    return "".join(rendered) if changed else text


def _python_dependency_frame_path(line: str) -> str:
    match = _PYTHON_FRAME_RE.match(line.rstrip("\r\n"))
    return match.group(1) if match else ""


def _node_dependency_frame_path(line: str) -> str:
    if not _NODE_FRAME_RE.match(line):
        return ""
    stripped = line.strip()
    if "(" in stripped and stripped.endswith(")"):
        location = stripped.rsplit("(", 1)[1].removesuffix(")")
    else:
        location = stripped.removeprefix("at ").rsplit(" ", 1)[-1]
    if not _NODE_FRAME_LOCATION_RE.search(location):
        return ""
    return location


def _is_python_frame_source_line(line: str) -> bool:
    stripped = line.lstrip(" \t")
    return (
        line.startswith((" ", "\t"))
        and stripped != line
        and not _PYTHON_FRAME_RE.match(line.rstrip("\r\n"))
    )


def _line_ending(line: str) -> str:
    if line.endswith("\r\n"):
        return "\r\n"
    if line.endswith("\n"):
        return "\n"
    return ""


def _path_parts(path: str) -> list[str]:
    normalized = path.replace("\\", "/").lower()
    normalized = re.sub(r"^[a-z]:", "", normalized)
    return [
        part
        for part in normalized.split("/")
        if part and part not in {".", ".."}
    ]


def _has_dependency_segment(
    path: str,
    project_parts: list[str],
    dependency_segments: set[str],
) -> bool:
    parts = _path_parts(path)
    if project_parts and parts[: len(project_parts)] == project_parts:
        parts = parts[len(project_parts) :]
    return any(part in dependency_segments for part in parts)
