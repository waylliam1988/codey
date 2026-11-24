"""Shared Tree-sitter grammars and checks for whitespace-assisted edits."""
from __future__ import annotations

import tree_sitter_javascript
import tree_sitter_typescript
from tree_sitter import Language, Node, Parser

from codey.toolchain.edit_text import TextEdit

ASSISTED_SUFFIXES = frozenset({".js", ".ts"})
_PROTECTED = frozenset({"template_string", "string", "jsx_text", "comment"})


def ecmascript_language(suffix: str) -> Language | None:
    if suffix in {".js", ".jsx", ".mjs", ".cjs"}:
        return Language(tree_sitter_javascript.language())
    if suffix == ".ts":
        return Language(tree_sitter_typescript.language_typescript())
    if suffix == ".tsx":
        return Language(tree_sitter_typescript.language_tsx())
    return None


def _protected_range(root: Node, first: int, last: int) -> bool:
    pending = [root]
    while pending:
        node = pending.pop()
        start, end = node.start_point.row, node.end_point.row
        if end < first or start > last:
            continue
        if start != end and node.type in _PROTECTED:
            return True
        pending.extend(node.named_children)
    return False


def safe_alignment(language: Language, source: str, edit: TextEdit) -> bool:
    parser = Parser(language)
    before = parser.parse(source.encode("utf-8")).root_node
    first = source.count("\n", 0, edit.start)
    last = source.count("\n", 0, max(edit.start, edit.end - 1))
    if before.has_error or _protected_range(before, first, last):
        return False
    updated = source[:edit.start] + edit.replacement + source[edit.end:]
    after = parser.parse(updated.encode("utf-8")).root_node
    last = first + edit.replacement.removesuffix("\n").count("\n")
    return not after.has_error and not _protected_range(after, first, last)


def valid_ecmascript(language: Language, source: str) -> bool:
    return not Parser(language).parse(source.encode("utf-8")).root_node.has_error
