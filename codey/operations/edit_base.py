"""Resolve a whole-file edit baseline from the original task receipt."""
from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import TYPE_CHECKING

from codey.toolchain.constants import FULL_READ_REFERENCE_HINT
from codey.types.json_value import JsonObject, JsonValue, decode_json_object, require_json_integer, require_json_string

if TYPE_CHECKING:
    from codey.operations.task_session import TaskSession


def _edit_base(session: TaskSession | None, project: Path, path: str, ref: str) -> tuple[str, str]:
    from codey.agents.protocol import canonical_project_path
    from codey.toolchain.result_schema import read_file_output_schema, validate_output
    result = session._memory_results.get(ref) if session is not None else None
    if result is None:
        raise ValueError("base_result_ref must identify a successful read_file receipt from this task. "
                         + FULL_READ_REFERENCE_HINT)
    if result.ok is not True or result.call.name not in {"read", "read_file"}:
        raise ValueError(f"base_result_ref points to {result.call.name} (ok={result.ok}), not a successful read_file. "
                         "Read the target file completely and use that read's result_ref. " + FULL_READ_REFERENCE_HINT)
    value = decode_json_object(result.canonical.get("value"))
    validate_output("read_file", read_file_output_schema(), value)
    canonical = canonical_project_path(project, path)
    if (canonical_project_path(project, require_json_string(value["path"], "read path")) != canonical
            or canonical_project_path(project, require_json_string(result.call.args["path"], "read argument path")) != canonical):
        raise ValueError("base_result_ref belongs to a different file")
    version = require_json_string(result.canonical.get("file_version", ""), "file version")
    if value["text_complete"] is not True:
        raise ValueError("whole-file update requires complete read_file text")
    if (not result.truncated and value["start_line"] == 1
            and value["next_offset"] == 0 and value["end_line"] == value["total_lines"]):
        return require_json_string(value["text"], "read text"), version
    if session is None or not version:
        raise ValueError("whole-file update requires a complete, untruncated read_file receipt")
    total = require_json_integer(value["total_lines"], "total_lines")
    pages: dict[int, dict[int, str]] = {}
    for other in session._memory_results.values():
        args_path = other.call.args.get("path")
        if (not other.ok or other.call.name not in {"read", "read_file"} or not isinstance(args_path, str)
                or canonical_project_path(project, args_path) != canonical):
            continue
        raw = other.canonical.get("value")
        if (isinstance(raw, dict) and other.canonical.get("file_version") == version
                and raw.get("text_complete") is True and raw.get("total_lines") == total):
            page = decode_json_object(raw)
            validate_output("read_file", read_file_output_schema(), page)
            if canonical_project_path(project, require_json_string(page["path"], "read path")) == canonical:
                _remember_page(pages, page, total)
    usable: dict[int, tuple[int, str]] = {}
    for start in sorted(pages, reverse=True):
        selected = next(((end, text) for end, text in sorted(pages[start].items(), reverse=True)
                         if end == total or end + 1 in usable), None)
        if selected is not None:
            usable[start] = selected
    chunks: list[str] = []
    offset = 1
    while offset <= total and offset in usable:
        stop, text = usable[offset]
        chunks.append(text)
        offset = stop + 1
    if offset != total + 1:
        reachable = {1}
        for start in sorted(pages):
            if start in reachable:
                reachable.update(end + 1 for end in pages[start])
        raise ValueError(f"whole-file update requires complete read_file pages from the same version; read missing line {max(reachable)}")
    return "".join(chunks), version


def _remember_page(pages: dict[int, dict[int, str]], page: JsonObject, total: int) -> None:
    start = require_json_integer(page["start_line"], "start_line")
    end = require_json_integer(page["end_line"], "end_line")
    text = require_json_string(page["text"], "read text")
    if (start <= end <= total and (end == total or text.endswith("\n"))
            and text.count("\n") + (not text.endswith("\n")) == end - start + 1
            and page["next_offset"] == (end + 1 if end < total else 0)):
        pages.setdefault(start, {})[end] = text


def edit_base_text(session: TaskSession | None, project: Path, path: str, ref: str) -> str:
    return _edit_base(session, project, path, ref)[0]


def edit_base_version(session: TaskSession | None, project: Path, path: str, ref: str) -> str:
    from codey.workspace.paths import content_hash

    text, version = _edit_base(session, project, path, ref)
    return version or content_hash(text)


def content_edit_version(session: TaskSession | None, project: Path, path: str, args: Mapping[str, JsonValue]) -> str | None:
    from codey.toolchain.runtime import safe_join

    target = safe_join(project, path)
    if "base_result_ref" in args:
        version = edit_base_version(session, project, path, require_json_string(args["base_result_ref"], "base_result_ref"))
        if not target.is_file():
            raise ValueError("whole-file update requires the referenced file to still exist")
        return version
    if target.exists():
        raise ValueError(f"existing file requires replacements or content with a complete read_file base_result_ref: {path}. "
                         "base_result_ref is missing. " + FULL_READ_REFERENCE_HINT)
    return None
