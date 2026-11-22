"""Bounded structural observations; edits remain owned by the ordinary patch path."""
from __future__ import annotations

import ast
from pathlib import Path

import tree_sitter_javascript
import tree_sitter_typescript
from tree_sitter import Language, Parser

from codey.toolchain.runtime import ToolOutcome, safe_join
from codey.types.json_value import JsonObject, JsonValue
from codey.workspace.paths import content_hash, ensure_not_symlink


def syntax_query(root: Path, rel: str, node_type: str, *, limit: int = 40) -> ToolOutcome:
    path = ensure_not_symlink(safe_join(root, rel))
    if path.stat().st_size > 1_000_000:
        return ToolOutcome.error("syntax query file exceeds one megabyte")
    data = path.read_bytes()
    text = data.decode("utf-8")
    matches: list[JsonValue] = []
    visited = 0
    if path.suffix.lower() == ".py":
        for item in ast.walk(ast.parse(text)):
            visited += 1
            if type(item).__name__ == node_type and isinstance(item, (ast.stmt, ast.expr)):
                matches.append({"type": node_type, "start_line": item.lineno, "end_line": item.end_lineno,
                                "text": (ast.get_source_segment(text, item) or "")[:240]})
            if len(matches) >= limit or visited >= 20000:
                break
    else:
        suffix = path.suffix.lower()
        if suffix in {".js", ".jsx", ".mjs", ".cjs"}:
            language = Language(tree_sitter_javascript.language())
        elif suffix in {".ts", ".tsx"}:
            language = Language(tree_sitter_typescript.language_tsx() if suffix == ".tsx" else tree_sitter_typescript.language_typescript())
        else:
            return ToolOutcome.error("syntax_query supports Python and JS/TS; use language server documentSymbol for other languages")
        tree = Parser(language).parse(data)
        pending = [tree.root_node]
        while pending and visited < 20000 and len(matches) < limit:
            node = pending.pop()
            visited += 1
            if node.type == node_type:
                matches.append({"type": node_type, "start_line": node.start_point.row + 1,
                    "end_line": node.end_point.row + 1, "text": data[node.start_byte:node.end_byte].decode("utf-8")[:240]})
            pending.extend(reversed(node.named_children))
    value: JsonObject = {"path": rel, "file_version": "raw:" + content_hash(text), "matches": matches,
                         "bounded": len(matches) == limit or visited >= 20000}
    return ToolOutcome(str(value), True, canonical={"value": value})
