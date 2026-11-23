"""Resolve a whole-file edit baseline from the original task receipt."""
from __future__ import annotations

from collections import Counter
from collections.abc import Mapping
from pathlib import Path
from typing import TYPE_CHECKING

from codey.toolchain.constants import FULL_READ_REFERENCE_HINT
from codey.types.json_value import JsonObject, JsonValue, decode_json_object, require_json_integer, require_json_string

if TYPE_CHECKING:
    import ast

    from codey.operations.kernel_protocol import TurnSnapshot
    from codey.operations.task_session import TaskSession
    from codey.providers.base import AssistantTurn
    from codey.runtime.core.models import ToolResult
    from codey.toolchain.tool_spec import ToolSpec

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


def _partial_edit_target(reply: str, snapshot: TurnSnapshot, session: TaskSession | None) -> tuple[str, tuple[str, ...]]:
    import re

    from codey.research.calculation import file_selector_key
    from codey.toolchain.syntax_query import python_definitions
    from codey.types.json_value import parse_json_value

    field = r'"(?:[^"\\]|\\.)*"'
    match = re.match(r'^\s*\{\s*"tool"\s*:\s*"edit"\s*,\s*"args"\s*:\s*\{\s*'
                     rf'(?:(?:"symbol"|"base_result_ref")\s*:\s*{field}\s*,\s*)*'
                     rf'"path"\s*:\s*({field})', reply)
    if session is None or not session.project or match is None:
        return "", ()
    try:
        path = file_selector_key(require_json_string(parse_json_value(match[1]), "edit path"))
        reference = dict(snapshot.symbol_read_refs).get(path)
        if reference is None:
            return "", ()
        source = edit_base_text(session, Path(session.project), path, reference)
        counts = Counter(node.name for node in python_definitions(source))
    except (ValueError, SyntaxError):
        return "", ()
    return path, tuple(sorted(name for name, count in counts.items() if count == 1))


def compact_symbol_retry(snapshot: TurnSnapshot, reply: AssistantTurn | str, *,
                         session: TaskSession | None = None) -> TurnSnapshot:
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
    path, symbols = _partial_edit_target(reply.text, snapshot, session)
    paths = [path] if path and symbols else [name for name, _ in snapshot.symbol_read_refs]
    domains: dict[str, object] = {"path": {"type": "string", "enum": paths}}
    if symbols:
        domains["symbol"] = {"type": "string", "enum": list(symbols)}
    single_target = (len(symbols) == 1 and snapshot.task_navigation_url is None
                     and snapshot.bound_source_url is None and snapshot.browser_references is None)
    mandatory = ("content",) if single_target else SYMBOL_EDIT_FIELDS
    parameters = tuple((key, domains.get(key, schema))
                       for key, schema in spec.parameters if key in {"path", "symbol", "content", "base_result_ref"})
    compact = freeze_spec_parameters(replace(spec, parameters=parameters, required=mandatory,
        description="Replace one Python definition in the observed file.", json_examples=()))
    if single_target:
        return refresh_snapshot_contract(replace(snapshot, tool_names=("edit",), nested_tool_names=(),
            frozen_specs=(compact,), custom_executors=tuple(row for row in snapshot.custom_executors if row[0] == "edit")))
    return refresh_snapshot_contract(replace(snapshot,
        frozen_specs=tuple(compact if item.name == "edit" else item for item in snapshot.frozen_specs)))


def is_compact_symbol_retry(snapshot: TurnSnapshot) -> bool:
    return any(spec.name == "edit" and (spec.required == SYMBOL_EDIT_FIELDS or _bound_edit_target(spec) is not None)
               for spec in snapshot.frozen_specs)


def bound_symbol_retry(snapshot: TurnSnapshot) -> tuple[str, str] | None:
    return _bound_edit_target(snapshot.model_specs[0]) if snapshot.tool_names == ("edit",) else None


def raw_symbol_content(text: str) -> str:
    import ast
    import re

    source = text.strip()
    if fenced := re.fullmatch(r"```(?:python|py)?[ \t]*\r?\n([\s\S]*?)\r?\n```", source):
        source = fenced[1]
    if re.match(r"^(?:async\s+def|def|class)\s+[^\W\d]", source):
        return source
    try:
        body = ast.parse(source).body
    except SyntaxError:
        return ""
    if (len(body) < 2 or not isinstance(fn := body[-1], (ast.FunctionDef, ast.AsyncFunctionDef))
            or not all(isinstance(node, (ast.Import, ast.ImportFrom)) for node in body[:-1])):
        return ""
    return _definition_with_local_imports(source, fn, body[:-1])


def _definition_with_local_imports(source: str, fn: ast.FunctionDef | ast.AsyncFunctionDef, imports: list[ast.stmt]) -> str:
    import ast

    if fn.decorator_list or fn.body[0].lineno == fn.lineno:
        return ""
    names: set[str] = set()
    for node in imports:
        if (not isinstance(node, (ast.Import, ast.ImportFrom))
                or isinstance(node, ast.ImportFrom) and node.module == "__future__"
                or any(alias.name == "*" for alias in node.names)):
            return ""
        names.update(alias.asname or alias.name.partition(".")[0] for alias in node.names)
    signature = (fn.args, *(() if fn.returns is None else (fn.returns,)))
    if (any(isinstance(node, ast.Name) and node.id in names for item in signature for node in ast.walk(item))
            or any(isinstance(node, (ast.Global, ast.Nonlocal)) and names.intersection(node.names) for node in ast.walk(fn))):
        return ""
    definition = ast.get_source_segment(source, fn)
    if definition is None:
        return ""
    lines = definition.splitlines(keepends=True)
    first = fn.body[0]
    offset = first.lineno - fn.lineno
    prefix = lines[offset][:first.col_offset]
    if ast.get_docstring(fn, clean=False) is not None and first.end_lineno is not None:
        offset = first.end_lineno - fn.lineno + 1
    if offset and not lines[offset - 1].endswith("\n"):
        lines[offset - 1] += "\n"
    lines[offset:offset] = [prefix + ast.unparse(node) + "\n" for node in imports]
    return "".join(lines)


def _bound_edit_target(spec: ToolSpec) -> tuple[str, str] | None:
    if spec.required != ("content",):
        return None
    from codey.toolchain.tool_spec import thaw_schema_value

    params = dict(spec.parameters)
    fields: list[str] = []
    for key in ("path", "symbol"):
        schema = thaw_schema_value(params.get(key))
        choices = schema.get("enum") if isinstance(schema, dict) else None
        if not isinstance(choices, list) or len(choices) != 1 or not isinstance(choices[0], str):
            return None
        fields.append(choices[0])
    return fields[0], fields[1]


def lower_compact_symbol_edit(args: JsonObject, spec: ToolSpec) -> JsonObject:
    if (target := _bound_edit_target(spec)) is None:
        return args
    return {"path": target[0], "symbol": target[1], **args}


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
