"""Use actual F821 diagnostics and observed module exports to narrow Python repairs."""
from __future__ import annotations

import ast
import keyword
import re
import sys
import sysconfig
from collections.abc import Callable
from dataclasses import dataclass, replace
from io import StringIO
from pathlib import Path
from typing import TYPE_CHECKING

from codey.operations.edit_base import edit_base_text, symbol_read_refs
from codey.runtime.core import cancellation
from codey.runtime.core.process_io import ProcessIO, ProcessOptions
from codey.toolchain.runtime import ToolOutcome
from codey.types.json_value import JsonObject, JsonValue, decode_json_object, parse_json_value
from codey.workspace.paths import ensure_not_symlink

if TYPE_CHECKING:
    from codey.operations.task_session import TaskSession


@dataclass(frozen=True)
class UndefinedName:
    name: str
    line: int
    column: int

    def payload(self) -> JsonObject:
        return {"code": "F821", "name": self.name, "line": self.line, "column": self.column}


def _diagnose(ruff: str, project: Path, path: str, text: str) -> tuple[UndefinedName, ...] | None:
    data = text.encode("utf-8")
    if len(data) > 65536:
        return None

    def send_input(endpoint: ProcessIO) -> Callable[[], None]:
        if not endpoint.write_input(data, eof=True, valid=lambda: True).complete:
            raise OSError("incomplete static-check input")
        return lambda: None

    try:
        proc = cancellation.run_process(
            [ruff, "check", "--isolated", "--no-cache", "--select", "F821", "--output-format", "json",
             "--stdin-filename", path, "-"], cwd=project, timeout=5, capture_limit_bytes=16000,
            options=ProcessOptions(stdin=True), on_started=send_input)
        if proc.returncode not in {0, 1}:
            return None
        rows = parse_json_value(proc.stdout)
        if not isinstance(rows, list):
            return None
        diagnostics: list[UndefinedName] = []
        for raw in rows:
            row = decode_json_object(raw)
            if row.get("code") != "F821":
                return None
            message = row.get("message")
            location = decode_json_object(row["location"])
            line, column = location.get("row"), location.get("column")
            if (not isinstance(message, str) or not (match := re.fullmatch(r"Undefined name `([^\x60]+)`", message))
                    or type(line) is not int or type(column) is not int):
                return None
            diagnostics.append(UndefinedName(match[1], line, column))
        return tuple(diagnostics)
    except (KeyError, OSError, TimeoutError, ValueError):
        return None


def _exports(text: str, symbol: str) -> bool:
    tree = ast.parse(text)
    declarations = [node for node in tree.body
                    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) and node.name == symbol]
    return (len(declarations) == 1 and isinstance(declarations[0], (ast.FunctionDef, ast.AsyncFunctionDef))
            and not declarations[0].decorator_list
            and not any(isinstance(node, ast.Name) and isinstance(node.ctx, (ast.Store, ast.Del)) and node.id == symbol
                        for node in ast.walk(tree)))


def _source_module(session: TaskSession, project: Path, path: str, symbol: str,
                   reads: dict[str, str]) -> tuple[str, str] | None:
    matches: list[tuple[str, str]] = []
    for other, ref in reads.items():
        candidate = Path(other)
        if (other == path or candidate.parent != Path(".") or not candidate.stem.isidentifier()
                or keyword.iskeyword(candidate.stem) or candidate.stem in sys.stdlib_module_names
                or candidate.name == "__init__.py"
                or (project / candidate.stem).exists()):
            continue
        try:
            observed = edit_base_text(session, project, other, ref)
            if not _exports(observed, symbol):
                continue
            current = ensure_not_symlink(project / other).read_text(encoding="utf-8")
            if not _exports(current, symbol):
                return None
        except (OSError, SyntaxError, UnicodeError, ValueError):
            continue
        matches.append((candidate.stem, ref))
    if len(matches) != 1:
        return None
    module, ref = matches[0]
    # Check for another local declaration, without adopting an unread module.
    siblings = sorted(project.glob("*.py"))
    if len(siblings) > 64:
        return None
    for sibling in siblings:
        if sibling.name in {path, f"{module}.py"}:
            continue
        try:
            safe = ensure_not_symlink(sibling)
            if safe.stat().st_size > 65536 or _exports(safe.read_text(encoding="utf-8"), symbol):
                return None
        except (OSError, SyntaxError, ValueError):
            return None
    return module, ref


def _insert_import(after: str, tree: ast.Module, module: str, name: str) -> str:
    lines = StringIO(after, newline="").readlines()
    anchor = tree.body[0].end_lineno or 0 if ast.get_docstring(tree, clean=False) is not None else 0
    for node in tree.body:
        if isinstance(node, ast.ImportFrom) and node.module == "__future__":
            anchor = node.end_lineno or anchor
    if not anchor:
        first = tree.body[0]
        decorators = first.decorator_list if isinstance(first, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)) else ()
        anchor = min((node.lineno for node in decorators), default=first.lineno) - 1
    newline = "\r\n" if lines[0].endswith("\r\n") else "\n"
    return "".join((*lines[:anchor], f"from {module} import {name}{newline}", *lines[anchor:]))


def python_edit_transform(session: TaskSession | None, project: Path, path: str, *,
                          allow_import: bool = True) -> Callable[[str, str], tuple[str, JsonObject]] | None:
    if session is None or not session.policy.allows("project.read") or Path(path).suffix.casefold() != ".py":
        return None
    reads = symbol_read_refs(session._memory_results, session.executed)
    ruff = Path(sysconfig.get_path("scripts")) / ("ruff.exe" if sys.platform == "win32" else "ruff")
    if path not in reads or not ruff.is_file():
        return None

    def transform(before: str, after: str) -> tuple[str, JsonObject]:
        if before == after:
            return after, {}
        fresh = _diagnose(str(ruff), project, path, after)
        if not fresh or (previous := _diagnose(str(ruff), project, path, before)) is None:
            return after, {}
        old_names = {diagnostic.name for diagnostic in previous}
        introduced = tuple(row for row in fresh if row.name not in old_names)
        if not introduced:
            return after, {}
        rows: list[JsonValue] = [row.payload() for row in introduced]
        audit: JsonObject = {"python_diagnostics": rows}
        names = {row.name for row in introduced}
        if not allow_import or Path(path).parent != Path(".") or len(names) != 1:
            return after, audit
        name = introduced[0].name
        tree = ast.parse(after)
        # Bind a newly unresolved direct function call, never arbitrary values.
        calls = {(node.func.lineno, node.func.col_offset) for node in ast.walk(tree)
                 if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == name}
        lines = after.splitlines()
        if not all((row.line, len(lines[row.line - 1][:row.column - 1].encode("utf-8"))) in calls for row in introduced):
            return after, audit
        source = _source_module(session, project, path, name, reads)
        if source is None:
            return after, audit
        module, ref = source
        repaired = _insert_import(after, tree, module, name)
        remaining = _diagnose(str(ruff), project, path, repaired)
        if remaining is None or any(row.name == name for row in remaining):
            return after, audit
        return repaired, {**audit, "python_import_binding": {"module": module, "name": name, "read_ref": ref}}

    return transform


def render_python_edit(outcome: ToolOutcome) -> ToolOutcome:
    if not outcome.ok:
        return outcome
    if isinstance(binding := outcome.audit.get("python_import_binding"), dict):
        text = f"Bound {binding['name']} to the observed module {binding['module']} (F821)."
    elif isinstance(rows := outcome.audit.get("python_diagnostics"), list):
        text = "New Python diagnostics: " + ", ".join(
            f"F821 {row['name']} at line {row['line']}, column {row['column']}" for row in map(decode_json_object, rows))
    else:
        return outcome
    return replace(outcome, model_text=f"{outcome.model_text}\n{text}")
