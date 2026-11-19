"""Prepare local edits from an immutable read receipt, using the existing writer."""
from __future__ import annotations

import ast
import io
import textwrap
import tokenize
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from codey.operations.edit_base import edit_base_text
from codey.operations.task_session import TaskSession
from codey.types.json_value import JsonObject, JsonValue, require_json_string


@dataclass(frozen=True)
class _PythonRegion:
    name: str
    start: int
    end: int


def _lines(text: str) -> list[str]:
    # Match read_file's LF line numbering, including a final unterminated line.
    return io.StringIO(text).readlines()


def region_content(session: TaskSession | None, project: Path, path: str,
                   args: Mapping[str, JsonValue]) -> str:
    baseline = edit_base_text(session, project, path,
                              require_json_string(args.get("base_result_ref"), "base_result_ref"))
    if Path(path).suffix != ".py":
        raise ValueError("named region edits require a Python file")
    name = require_json_string(args.get("region"), "region")
    matches = [region for region in _python_regions(baseline) if region.name == name]
    if len(matches) != 1:
        raise ValueError(f"region {name!r} must identify exactly one Python class or function in the referenced read")
    start, end = matches[0].start, matches[0].end
    lines = _lines(baseline)
    content = require_json_string(args.get("content"), "content")
    if args.get("relative_indent") is True:
        content = _relative_content("".join(lines[start - 1:end]), content)
    if content and end < len(lines) and not content.endswith("\n"):
        content += "\n"
    prepared = "".join(lines[:start - 1]) + content + "".join(lines[end:])
    if args.get("relative_indent") is True:
        try:
            ast.parse(prepared, filename=path)
        except SyntaxError as exc:
            raise ValueError(f"relative_indent would produce invalid Python at line {exc.lineno}: {exc.msg}; no edit applied") from exc
    return prepared


def _safe_indentation(text: str) -> None:
    if any("\t" in line[:len(line) - len(line.lstrip())] for line in _lines(text)):
        raise ValueError("relative_indent cannot adjust tabs; supply exact indentation without relative_indent")
    try:
        for token in tokenize.generate_tokens(io.StringIO(text).readline):
            string = token.type == tokenize.STRING or tokenize.tok_name[token.type].startswith("FSTRING")
            if string and token.start[0] != token.end[0]:
                raise ValueError("relative_indent cannot adjust multiline strings; supply exact content without relative_indent")
    except (tokenize.TokenError, IndentationError) as exc:
        raise ValueError("relative_indent cannot safely tokenize this region; supply exact content without relative_indent") from exc


def _relative_content(original: str, replacement: str) -> str:
    _safe_indentation(replacement)
    # The named AST region starts at its decorator/definition, never inside a
    # string. Old body text is replaced, so its literal indentation is irrelevant.
    first = _lines(original)[0]
    prefix = first[:len(first) - len(first.lstrip())]
    if prefix.strip(" "):
        raise ValueError("relative_indent target must use spaces; supply exact content without relative_indent")
    return textwrap.indent(textwrap.dedent(replacement), prefix)


def _python_regions(text: str) -> list[_PythonRegion]:
    try:
        tree = ast.parse(text)
    except SyntaxError:
        return []
    regions: list[_PythonRegion] = []

    def visit(nodes: list[ast.stmt], prefix: str = "") -> None:
        for node in nodes:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) and node.end_lineno is not None:
                name = prefix + node.name
                start = min([node.lineno, *(item.lineno for item in node.decorator_list)])
                regions.append(_PythonRegion(name, start, node.end_lineno))
                if isinstance(node, ast.ClassDef):
                    visit(node.body, name + ".")
    visit(tree.body)
    return regions


def python_region_hints(text: str) -> list[JsonObject]:
    """Derive navigation from the same parser used by editing; no region registry."""
    return [{"region": region.name, "start_line": region.start, "end_line": region.end}
            for region in _python_regions(text)[:24]]
