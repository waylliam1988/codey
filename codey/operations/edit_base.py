"""Resolve a whole-file edit baseline from the original task receipt."""
from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import TYPE_CHECKING

from codey.toolchain.constants import FULL_READ_REFERENCE_HINT
from codey.types.json_value import JsonValue, decode_json_object, require_json_string

if TYPE_CHECKING:
    from codey.operations.task_session import TaskSession


def edit_base_text(session: TaskSession | None, project: Path, path: str, ref: str) -> str:
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
    if (result.truncated or not value["text_complete"] or value["start_line"] != 1
            or value["next_offset"] != 0 or value["end_line"] != value["total_lines"]):
        raise ValueError("whole-file update requires a complete, untruncated read_file receipt")
    canonical = canonical_project_path(project, path)
    if (canonical_project_path(project, require_json_string(value["path"], "read path")) != canonical
            or canonical_project_path(project, require_json_string(result.call.args["path"], "read argument path")) != canonical):
        raise ValueError("base_result_ref belongs to a different file")
    return require_json_string(value["text"], "read text")


def edit_base_version(session: TaskSession | None, project: Path, path: str, ref: str) -> str:
    from codey.workspace.paths import content_hash

    return content_hash(edit_base_text(session, project, path, ref))


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
