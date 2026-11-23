"""Resolve a whole-file edit baseline from the original task receipt."""
from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import TYPE_CHECKING

from codey.toolchain.constants import FULL_READ_REFERENCE_HINT
from codey.types.json_value import JsonObject, JsonValue, decode_json_object, require_json_integer, require_json_string

if TYPE_CHECKING:
    from codey.operations.kernel_protocol import TurnSnapshot
    from codey.operations.task_session import TaskSession
    from codey.providers.base import AssistantTurn
    from codey.runtime.core.models import ToolResult

SYMBOL_EDIT_FIELDS = ("path", "symbol", "content")


def symbol_read_refs(results: Mapping[str, ToolResult], executed: Mapping[str, object]) -> dict[str, str]:
    from codey.research.calculation import file_selector_key

    refs: dict[str, str] = {}
    for ref, result in results.items():
        raw = result.canonical.get("value")
        if (ref in executed and result.ok and result.call.name in {"read", "read_file"}
                and isinstance(raw, dict) and not result.truncated and raw.get("text_complete") is True
                and raw.get("start_line") == 1 and raw.get("next_offset") == 0
                and raw.get("end_line") == raw.get("total_lines")
                and isinstance(path := raw.get("path"), str) and Path(path).suffix.casefold() == ".py"):
            refs[file_selector_key(path)] = ref
    return refs


def bind_symbol_read(args: JsonObject, reads: Mapping[str, str]) -> JsonObject:
    from codey.research.calculation import file_selector_key

    if "symbol" not in args or "base_result_ref" in args:
        return args
    path = require_json_string(args.get("path"), "edit path")
    ref = reads.get(file_selector_key(path))
    if ref is None:
        raise ValueError("Read the complete target file before editing its symbol.")
    return {**args, "base_result_ref": ref}


def compact_symbol_retry(snapshot: TurnSnapshot, reply: AssistantTurn | str) -> TurnSnapshot:
    """Narrow a truncated edit retry without accepting any of its arguments."""
    import re
    from dataclasses import replace

    from codey.operations.kernel_protocol import refresh_snapshot_contract
    from codey.providers.base import AssistantTurn, TurnFinish
    from codey.toolchain.tool_spec import freeze_spec_parameters

    spec = next((item for item in snapshot.model_specs if item.name == "edit"), None)
    if (not isinstance(reply, AssistantTurn) or reply.finish is not TurnFinish.OUTPUT_LIMIT or reply.tool_calls
            or re.match(r'^\s*\{\s*"tool"\s*:\s*"edit"', reply.text) is None
            or not snapshot.symbol_read_refs or spec is None or snapshot.input_binding is not None
            or "symbol" not in dict(spec.parameters)):
        return snapshot
    parameters = tuple((key, {"type": "string", "enum": [path for path, _ in snapshot.symbol_read_refs]}
                       if key == "path" else schema)
                       for key, schema in spec.parameters if key in {"path", "symbol", "content", "base_result_ref"})
    compact = freeze_spec_parameters(replace(spec, parameters=parameters, required=SYMBOL_EDIT_FIELDS,
        description="Replace one Python definition in the observed file.", json_examples=()))
    return refresh_snapshot_contract(replace(snapshot,
        frozen_specs=tuple(compact if item.name == "edit" else item for item in snapshot.frozen_specs)))


def is_compact_symbol_retry(snapshot: TurnSnapshot) -> bool:
    return any(spec.name == "edit" and spec.required == SYMBOL_EDIT_FIELDS for spec in snapshot.frozen_specs)


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


def symbol_edit_args(session: TaskSession | None, project: Path, path: str, args: JsonObject) -> JsonObject:
    """Expand a replacement definition using only the versioned read receipt."""
    import ast
    from io import StringIO

    from codey.toolchain.syntax_query import python_definitions, reindent_python_source

    if session is None or Path(path).suffix.lower() != ".py":
        raise ValueError("symbol edits require a task-read Python file")
    reference = args.get("base_result_ref")
    if not isinstance(reference, str):
        raise ValueError("symbol edits require a complete read of the target file")
    before = edit_base_text(session, project, path, reference)
    name = require_json_string(args["symbol"], "symbol")
    replacement = reindent_python_source(require_json_string(args["content"], "replacement definition"))
    try:
        matches = [node for node in python_definitions(before) if node.name == name]
        body = ast.parse(replacement).body
    except SyntaxError as exc:
        raise ValueError(f"symbol edit requires valid Python: {exc.msg}") from exc
    if len(matches) != 1:
        raise ValueError(f"symbol {name!r} must occur exactly once in the original file")
    original = matches[0]
    if (len(body) != 1 or not isinstance(node := body[0], (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
            or node.name != name or node.decorator_list or original.end_lineno is None):
        raise ValueError("supply one definition of the selected symbol, without its existing decorators")
    old = ast.get_source_segment(before, original)
    if not old:
        raise ValueError("symbol has no original source range")
    source_lines = StringIO(before, newline="").readlines()
    line = source_lines[original.lineno - 1]
    prefix = line.encode("utf-8")[:original.col_offset].decode("utf-8")
    newline = "\r\n" if line.endswith("\r\n") else "\r" if line.endswith("\r") else "\n"
    replacement = reindent_python_source(replacement, prefix).removeprefix(prefix).replace("\n", newline)
    start = sum(map(len, source_lines[:original.lineno - 1])) + len(prefix)
    content = before[:start] + replacement + before[start + len(old):]
    try:
        after = python_definitions(content)
    except SyntaxError as exc:
        raise ValueError(f"symbol replacement produces invalid Python: {exc.msg}") from exc
    if sum(node.name == name for node in after) != 1:
        raise ValueError("replacement introduces another definition of the selected symbol")
    return {"path": path, "base_result_ref": reference, "content": content}


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
