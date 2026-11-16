"""Diagnose definite value-shape errors; authorize only settled read-only reuse."""
from __future__ import annotations

from collections.abc import Mapping, Sequence

import tree_sitter_javascript
from tree_sitter import Language, Node, Parser

from codey.codemode.types import ToolCatalogSnapshot
from codey.runtime.core.models import ToolResult
from codey.types.json_value import JsonObject, canonical_json_object

_STRING_PROPERTIES = frozenset((
    "length", "at", "charAt", "charCodeAt", "codePointAt", "concat", "endsWith", "includes", "indexOf",
    "isWellFormed", "lastIndexOf", "localeCompare", "match", "matchAll", "normalize", "padEnd", "padStart",
    "repeat", "replace", "replaceAll", "search", "slice", "split", "startsWith", "substring", "toLocaleLowerCase",
    "toLocaleUpperCase", "toLowerCase", "toString", "toUpperCase", "toWellFormed", "trim", "trimEnd", "trimStart", "valueOf",
))


def readonly_settled(parent_id: str, refs: Sequence[str], results: Mapping[str, ToolResult],
                     catalog: ToolCatalogSnapshot) -> bool:
    entries = {entry.name: entry for entry in catalog.entries}
    owned = {ref for ref, result in results.items() if result.call.call_id.startswith(parent_id + ":child:")}
    return bool(refs) and set(refs) == owned and len(refs) == len(owned) and all(
        (result := results.get(ref)) is not None and result.ok
        and result.call.call_id == f"{parent_id}:child:{index}"
        and (entry := entries.get(result.call.name)) is not None and entry.read_only
        for index, ref in enumerate(refs))


def shape_diagnostic(code: str, catalog: ToolCatalogSnapshot, *, receipt_replay: bool = True) -> str:
    source = code.encode("utf-8")
    root = Parser(Language(tree_sitter_javascript.language())).parse(source).root_node
    if root.has_error:
        return ""
    schemas = {entry.name: canonical_json_object(entry.output_schema) for entry in catalog.entries}
    bindings: dict[str, JsonObject] = {}

    def text(node: Node) -> str:
        return source[node.start_byte:node.end_byte].decode("utf-8")

    def schema(node: Node | None) -> JsonObject | None:
        if node is None:
            return None
        if node.type == "identifier":
            return bindings.get(text(node))
        if node.type in {"await_expression", "parenthesized_expression"}:
            return schema(node.named_children[0]) if len(node.named_children) == 1 else None
        if node.type == "call_expression":
            function, args = node.child_by_field_name("function"), node.child_by_field_name("arguments")
            if function is not None and text(function) == "unwrap" and args is not None and len(args.named_children) == 1:
                result = schema(args.named_children[0])
                properties = result.get("properties") if result is not None else None
                value = properties.get("value") if isinstance(properties, dict) else None
                return value if isinstance(value, dict) else None
            if function is not None and function.type == "member_expression":
                owner, name = function.child_by_field_name("object"), function.child_by_field_name("property")
                if owner is not None and name is not None and text(owner) == "tools" and text(name) in schemas:
                    return {"type": "object", "properties": {"value": schemas[text(name)]}}
        if node.type == "member_expression":
            value, name = schema(node.child_by_field_name("object")), node.child_by_field_name("property")
            properties = value.get("properties") if value is not None else None
            child = properties.get(text(name)) if isinstance(properties, dict) and name is not None else None
            return child if isinstance(child, dict) else None
        return None

    # Only immutable, straight-line top-level bindings are inferred. Unknown
    # scopes, assignments and dynamic properties produce no repair authority.
    for statement in root.named_children:
        if statement.type == "lexical_declaration" and text(statement).startswith("const "):
            for declaration in statement.named_children:
                name, value = declaration.child_by_field_name("name"), declaration.child_by_field_name("value")
                inferred = schema(value)
                if name is not None and name.type == "identifier" and inferred is not None:
                    bindings[text(name)] = inferred
        pending = [statement]
        while pending:
            node = pending.pop()
            if node.type in {"function_expression", "arrow_function", "function_declaration"}:
                continue
            pending.extend(reversed(node.named_children))
            if node.type != "member_expression":
                continue
            owner, name = node.child_by_field_name("object"), node.child_by_field_name("property")
            shape = schema(owner)
            if shape is not None and name is not None and shape.get("type") == "string" and text(name) not in _STRING_PROPERTIES:
                return (f"{text(owner) if owner is not None else 'value'} is a string according to the invoked tool's "
                        f"output schema; property {text(name)!r} is not its data. Use the string directly, "
                        "or its standard string methods. " + ("Keep all original tool calls and arguments unchanged."
                        if receipt_replay else "Continue from the settled child receipts; do not repeat completed actions."))
    return ""
