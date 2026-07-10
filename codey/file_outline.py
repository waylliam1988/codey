"""Small source-outline extraction for navigation, not editing.

The outline is deliberately shallow: it exposes line-number hints for imports,
top-level declarations, methods, tests, and common web routes. It never returns
function bodies and should not be used as source text for edits.
"""

from __future__ import annotations

import ast
import re
from pathlib import PurePosixPath
from typing import Iterable


OUTLINE_MAX_FILE_BYTES = 256 * 1024
OUTLINE_MAX_ITEMS = 80
OUTLINE_MAX_CHARS = 8_000
OUTLINE_SUPPORTED_SUFFIXES = {
    ".js",
    ".jsx",
    ".mjs",
    ".cjs",
    ".py",
    ".ts",
    ".tsx",
}
JS_ROUTE_METHODS = "get|post|put|patch|delete|options|head|use"

_JS_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    (
        "import",
        re.compile(r"^\s*import\s+(?:type\s+)?(?:.+?\s+from\s+)?['\"]([^'\"]+)['\"]"),
    ),
    (
        "export",
        re.compile(
            r"^\s*export\s+(?:default\s+)?(?:async\s+)?"
            r"(?:function|class|const|let|var)\s+([A-Za-z_$][\w$]*)"
        ),
    ),
    (
        "function",
        re.compile(r"^\s*(?:async\s+)?function\s+([A-Za-z_$][\w$]*)\s*\("),
    ),
    (
        "class",
        re.compile(r"^\s*class\s+([A-Za-z_$][\w$]*)\b"),
    ),
    (
        "arrow",
        re.compile(
            r"^\s*(?:export\s+)?(?:const|let|var)\s+([A-Za-z_$][\w$]*)\s*="
            r"\s*(?:async\s*)?(?:\([^)]*\)|[A-Za-z_$][\w$]*)\s*=>"
        ),
    ),
    (
        "route",
        re.compile(
            rf"\b([A-Za-z_$][\w$]*(?:Router|router|app|server)?)"
            rf"\s*\.\s*({JS_ROUTE_METHODS})\s*\(\s*['\"]([^'\"]+)['\"]",
            re.IGNORECASE,
        ),
    ),
)


def build_file_outline(text: str, rel: str) -> str:
    suffix = PurePosixPath(rel).suffix.lower()
    if suffix == ".py":
        items, truncated = outline_python(text)
    elif suffix in OUTLINE_SUPPORTED_SUFFIXES:
        items, truncated = outline_javascript_like(text)
    else:
        return (
            f"File outline: {rel}\n"
            "- outline unavailable for this file type; use grep/read_file"
        )
    header = [
        f"File outline: {rel}",
        "- outline only; use read_file before editing",
        "- line numbers are navigation hints, not source text",
    ]
    if not items:
        header.append("- no outline items found; use grep/read_file")
    else:
        header.extend(items)
    if truncated:
        header.append("- outline truncated; use grep/read_file for narrower inspection")
    output = "\n".join(header)
    if len(output) <= OUTLINE_MAX_CHARS:
        return output
    return output[:OUTLINE_MAX_CHARS].rstrip() + "\n- outline truncated by character budget"


def outline_python(text: str) -> tuple[list[str], bool]:
    try:
        tree = ast.parse(text)
    except SyntaxError as exc:
        line = exc.lineno or 1
        return ([f"- syntax error near line {line}; use read_file"], False)

    items: list[str] = []
    truncated = False

    def add(item: str) -> None:
        nonlocal truncated
        if len(items) >= OUTLINE_MAX_ITEMS:
            truncated = True
            return
        items.append(item)

    for node in tree.body:
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            add(_python_import_item(node))
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            add(f"- function {node.name}{_python_args(node)} line {node.lineno}")
        elif isinstance(node, ast.ClassDef):
            add(f"- class {node.name} line {node.lineno}")
            for child in node.body:
                if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    add(
                        f"- method {node.name}.{child.name}"
                        f"{_python_args(child)} line {child.lineno}"
                    )
    return items, truncated


def outline_javascript_like(text: str) -> tuple[list[str], bool]:
    items: list[str] = []
    seen: set[tuple[int, str]] = set()
    truncated = False

    def add(line_no: int, item: str) -> None:
        nonlocal truncated
        key = (line_no, item)
        if key in seen:
            return
        seen.add(key)
        if len(items) >= OUTLINE_MAX_ITEMS:
            truncated = True
            return
        items.append(item)

    for line_no, line in enumerate(text.splitlines(), start=1):
        for kind, pattern in _JS_PATTERNS:
            match = pattern.search(line)
            if not match:
                continue
            if kind == "import":
                add(line_no, f"- import {match.group(1)} line {line_no}")
            elif kind == "export":
                add(line_no, f"- export {match.group(1)} line {line_no}")
            elif kind == "function":
                add(line_no, f"- function {match.group(1)} line {line_no}")
            elif kind == "class":
                add(line_no, f"- class {match.group(1)} line {line_no}")
            elif kind == "arrow":
                add(line_no, f"- arrow {match.group(1)} line {line_no}")
            elif kind == "route":
                owner, method, route = match.groups()
                add(line_no, f"- route {owner}.{method.upper()} {route} line {line_no}")
        if truncated:
            break
    return items, truncated


def _python_import_item(node: ast.Import | ast.ImportFrom) -> str:
    if isinstance(node, ast.Import):
        names = ", ".join(_clip_name(alias.name) for alias in node.names[:4])
        return f"- import {names} line {node.lineno}"
    module = "." * node.level + (node.module or "")
    imported = ", ".join(_clip_name(alias.name) for alias in node.names[:4])
    return f"- import from {module or '.'} {imported} line {node.lineno}"


def _python_args(node: ast.FunctionDef | ast.AsyncFunctionDef) -> str:
    args: Iterable[ast.arg] = (*node.args.posonlyargs, *node.args.args)
    names = [arg.arg for arg in args]
    if node.args.vararg is not None:
        names.append("*" + node.args.vararg.arg)
    if node.args.kwarg is not None:
        names.append("**" + node.args.kwarg.arg)
    if len(names) > 4:
        names = [*names[:4], "..."]
    return "(" + ", ".join(names) + ")"


def _clip_name(value: str) -> str:
    return value if len(value) <= 80 else value[:77] + "..."
