"""Version-bound semantic queries and read-only rename plans."""
from __future__ import annotations

import json
import re
import shutil
from collections.abc import Sequence
from pathlib import Path

from codey.toolchain.language_server import LanguageServer, document_path, lookup, server_for
from codey.toolchain.runtime import ToolOutcome, safe_join
from codey.types.json_value import JsonObject, JsonValue, decode_json_object, require_json_integer, require_json_string
from codey.workspace.paths import content_hash, ensure_not_symlink, path_hash
from codey.workspace.revision import scan_workspace_fingerprint


def pyright_server() -> str | None:
    return shutil.which("pyright-langserver")


def _lookup(argv: Sequence[str], root: Path, path: Path, text: str, line: int, character: int, *,
            language: str = "python", operation: str = "definition", new_name: str = "") -> JsonValue:
    return lookup(LanguageServer(argv[0], language, tuple(argv[1:])), root, path, text, line, character,
                  operation=operation, new_name=new_name)


def _locations(root: Path, raw: JsonValue) -> list[JsonValue]:
    locations: list[JsonValue] = []
    for item in raw if isinstance(raw, list) else [raw] if isinstance(raw, dict) else []:
        row = decode_json_object(item)
        uri = row.get("uri", row.get("targetUri"))
        if not isinstance(uri, str):
            continue
        path = document_path(uri)
        if path is None or not path.is_relative_to(root):
            continue
        span = decode_json_object(row.get("range", row.get("targetSelectionRange")))
        start = decode_json_object(span["start"])
        locations.append({"path": path.relative_to(root).as_posix(),
            "line": require_json_integer(start["line"], "definition line") + 1,
            "column_utf16": require_json_integer(start["character"], "definition column") + 1})
    return locations


def _position(text: str, position: JsonObject) -> int:
    line = require_json_integer(position.get("line"), "line")
    column = require_json_integer(position.get("character"), "character")
    lines = text.splitlines(keepends=True)
    if line == len(lines) and column == 0:
        return len(text)
    if not 0 <= line < len(lines) or column < 0:
        raise ValueError("language edit is outside the document")
    prefix = lines[line].rstrip("\r\n").encode("utf-16-le")
    if column * 2 > len(prefix):
        raise ValueError("language edit column is outside the line")
    return sum(map(len, lines[:line])) + len(prefix[:column * 2].decode("utf-16-le"))


def _rename_plan(root: Path, raw: JsonValue, symbol: str) -> list[JsonValue]:
    row = decode_json_object(raw)
    changes = decode_json_object(row.get("changes", {}))
    documents = row.get("documentChanges", [])
    if not isinstance(documents, list):
        raise ValueError("invalid language edit documents")
    for value in documents:
        document = decode_json_object(value)
        identity = decode_json_object(document.get("textDocument"))
        uri = require_json_string(identity.get("uri"), "edit URI")
        if uri in changes:
            raise ValueError("duplicate language edit document")
        changes[uri] = document.get("edits", [])
    if not 1 <= len(changes) <= 8:
        raise ValueError("rename requires one to eight existing workspace files")
    planned: list[JsonValue] = []
    for uri, edits in changes.items():
        path = document_path(uri)
        if path is None or not path.is_relative_to(root):
            raise ValueError("rename path escapes workspace")
        path = ensure_not_symlink(path)
        if path.stat().st_size > 512 * 1024 or not isinstance(edits, list):
            raise ValueError("rename path escapes workspace or exceeds edit limits")
        before = path.read_bytes().decode("utf-8")
        spans: list[tuple[int, int, str]] = []
        for item in edits:
            edit = decode_json_object(item)
            span = decode_json_object(edit.get("range"))
            start = _position(before, decode_json_object(span.get("start")))
            stop = _position(before, decode_json_object(span.get("end")))
            if start >= stop or before[start:stop] != symbol:
                raise ValueError("rename anchor differs from the queried symbol")
            replacement = require_json_string(edit.get("newText"), "new text")
            spans.append((start, stop, replacement))
        after, end = before, len(before)
        for start, stop, replacement in sorted(spans, reverse=True):
            if stop > end:
                raise ValueError("overlapping language edits")
            after = after[:start] + replacement + after[stop:]
            end = start
        planned.append({"path": path.relative_to(root).as_posix(), "expected_version": content_hash(before),
                        "replacements": [{"old_string": before, "new_string": after}]})
    return planned


def semantic_definition(root: Path, rel: str, line: int = 1, symbol: str = "", *,
                        operation: str = "definition", new_name: str = "") -> ToolOutcome:
    root = root.resolve()
    path = ensure_not_symlink(safe_join(root, rel))
    server = server_for(path)
    if path.suffix.lower() == ".py":
        executable = pyright_server()
        server = LanguageServer(executable, "python") if executable else None
    if server is None:
        return ToolOutcome.error("Semantic query requires the matching language server on PATH (Python requires pyright-langserver)")
    if path.stat().st_size > 1_000_000 or Path(server.command).resolve().is_relative_to(root):
        return ToolOutcome.error("semantic query requires a bounded file and a server outside the project")
    text = path.read_bytes().decode("utf-8")
    version = content_hash(text)
    lines = text.splitlines()
    character = 0
    if operation not in {"diagnostics", "documentSymbol"}:
        if not 1 <= line <= len(lines) or not symbol.isidentifier():
            return ToolOutcome.error("semantic query requires a valid line and identifier")
        occurrences = list(re.finditer(r"(?<!\w)" + re.escape(symbol) + r"(?!\w)", lines[line - 1]))
        if len(occurrences) != 1:
            return ToolOutcome.error("the identifier must occur exactly once on the requested line")
        character = len(lines[line - 1][:occurrences[0].start()].encode("utf-16-le")) // 2
    if operation == "rename" and not new_name.isidentifier():
        return ToolOutcome.error("rename requires a new identifier")
    workspace = scan_workspace_fingerprint(root) if operation == "rename" else ""
    raw = _lookup((server.command, *server.arguments), root, path, text, line - 1, character,
                  language=server.language, operation=operation, new_name=new_name)
    if path_hash(path, preserve_newlines=True) != version or workspace and scan_workspace_fingerprint(root) != workspace:
        return ToolOutcome.error("the queried workspace or document changed while the language server was running; reread it")
    result: JsonObject = {"path": rel, "file_version": "raw:" + version, "line": line, "symbol": symbol,
                          "operation": operation, "language": server.language}
    if operation in {"definition", "references"}:
        result["definitions" if operation == "definition" else "references"] = _locations(root, raw)
    elif operation == "rename":
        result["rename_plan"] = _rename_plan(root, raw, symbol)
    else:
        result["result"] = raw
    return ToolOutcome(json.dumps(result, ensure_ascii=False), True, canonical=result)
