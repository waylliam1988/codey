"""Diagnose definite value-shape errors; authorize only settled read-only reuse."""
from __future__ import annotations

import json
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass, replace
from typing import Literal

import tree_sitter_javascript
from tree_sitter import Language, Node, Parser

from codey.codemode.protocol import CodeModeInputError, SourceDiagnostic, _diagnostic, _node_text, parse_codemode_args
from codey.codemode.types import ToolCatalogSnapshot
from codey.runtime.core.models import ToolCall, ToolPlan, ToolResult
from codey.types.json_value import JsonObject, canonical_json_object

_STRING_PROPERTIES = frozenset((
    "length", "at", "charAt", "charCodeAt", "codePointAt", "concat", "endsWith", "includes", "indexOf",
    "isWellFormed", "lastIndexOf", "localeCompare", "match", "matchAll", "normalize", "padEnd", "padStart",
    "repeat", "replace", "replaceAll", "search", "slice", "split", "startsWith", "substring", "toLocaleLowerCase",
    "toLocaleUpperCase", "toLowerCase", "toString", "toUpperCase", "toWellFormed", "trim", "trimEnd", "trimStart", "valueOf",
))
_OBJECT_PROPERTIES = frozenset({
    "constructor", "__proto__", "hasOwnProperty", "isPrototypeOf", "propertyIsEnumerable", "toLocaleString",
    "toString", "valueOf", "__defineGetter__", "__defineSetter__", "__lookupGetter__", "__lookupSetter__",
})
_EXPRESSIONS = frozenset({
    "lexical_declaration", "variable_declarator", "return_statement", "expression_statement",
    "await_expression", "parenthesized_expression", "call_expression", "arguments",
    "member_expression", "subscript_expression", "object", "pair", "array", "identifier",
})
_MUTATION_BOUNDARIES = frozenset({
    "assignment_expression", "augmented_assignment_expression", "update_expression", "unary_expression",
    "if_statement", "statement_block", "for_statement", "for_in_statement", "while_statement", "do_statement",
    "try_statement", "switch_statement", "labeled_statement", "binary_expression", "ternary_expression",
    "sequence_expression", "template_string", "new_expression", "spread_element", "object_pattern", "array_pattern",
    "class", "class_declaration",
})


@dataclass(frozen=True)
class _ToolValue:
    name: str
    schema: JsonObject
    stage: Literal["promise", "receipt", "value"] = "promise"
    receipt: Node | None = None
    declaration: Node | None = None
    csv: bool = False


@dataclass(frozen=True)
class _SourceEdit:
    start: int
    end: int
    replacement: bytes


def _nodes(root: Node) -> Iterator[Node]:
    stack = [root]
    while stack:
        node = stack.pop()
        yield node
        stack.extend(reversed(node.named_children))


def _variants(schema: JsonObject) -> tuple[JsonObject, ...]:
    variants = schema.get("anyOf", schema.get("oneOf"))
    if isinstance(variants, list) and variants:
        return tuple(branch for item in variants for branch in _variants(item if isinstance(item, dict) else {}))
    return (schema,)


def _fields(schema: JsonObject) -> JsonObject:
    properties = schema.get("properties")
    return properties if isinstance(properties, dict) else {}


def _lacks_field(schema: JsonObject, field: str) -> bool:
    return field not in _fields(schema) and (
        schema.get("type") == "string" and field not in _STRING_PROPERTIES
        or schema.get("type") == "object" and schema.get("additionalProperties") is False
        and not schema.get("patternProperties")
    )


def _csv_row_field(schema: JsonObject) -> str | None:
    fields: list[str] = []
    for branch in _variants(schema):
        properties = _fields(branch)
        format_schema = properties.get("format")
        if not isinstance(format_schema, dict) or format_schema.get("enum") != ["csv"]:
            continue
        required = branch.get("required")
        if not isinstance(required, list):
            return None
        candidates = [field for field, child in properties.items()
                      if field in required and isinstance(child, dict) and child.get("type") == "array"
                      and isinstance(item := child.get("items"), dict) and item.get("type") == "object"]
        if len(candidates) != 1:
            return None
        fields.extend(candidates)
    unique = set(fields)
    return unique.pop() if len(unique) == 1 else None


class _ShapeCheck:
    def __init__(self, source: bytes, catalog: ToolCatalogSnapshot, *, preflight: bool,
                 inputs: Mapping[str, object], rewrite: bool) -> None:
        self.source = source
        self.preflight = preflight
        # Browser flow has an alternate result shape. Its ordinary output schema cannot prove absence.
        self.schemas = {entry.name: {} if "x-codey-browser-flow" in entry.parameters
                        else canonical_json_object(entry.output_schema) for entry in catalog.entries}
        self.bindings: dict[str, _ToolValue] = {}
        self.values: dict[Node, _ToolValue] = {}
        self.inputs = inputs
        self.rewrite = rewrite
        self.edits: list[_SourceEdit] = []
        self.receipt_bindings: dict[Node, str] = {}
        self.corrections = 0

    def text(self, node: Node) -> str:
        return _node_text(node, self.source)

    def field(self, node: Node) -> tuple[Node, Node, str] | None:
        if node.type not in {"member_expression", "subscript_expression"}:
            return None
        owner = node.child_by_field_name("object")
        key = node.child_by_field_name("property" if node.type == "member_expression" else "index")
        if owner is None or key is None:
            return None
        if key.type == "property_identifier" and "\\" not in self.text(key):
            return owner, key, self.text(key)
        if key.type == "string" and key.named_child_count == 1:
            fragment = key.named_children[0]
            if fragment.type == "string_fragment":
                return owner, key, self.text(fragment)
        return None

    def is_csv(self, args: Node | None) -> bool:
        if args is None:
            return False
        arguments = [arg for arg in args.named_children if arg.type != "comment"]
        if len(arguments) != 1:
            return False
        argument = arguments[0]
        if argument.type == "identifier" and self.text(argument) == "inputs":
            return self.inputs.get("format") == "csv"
        if argument.type != "object":
            return False
        explicit_formats: list[bool] = []
        for pair in argument.named_children:
            if pair.type == "comment":
                continue
            if pair.type == "shorthand_property_identifier" and self.text(pair) != "format":
                continue
            if pair.type != "pair":
                return False
            key = pair.child_by_field_name("key")
            if key is None or key.type not in {"property_identifier", "string"} or "\\" in self.text(key):
                return False  # An indirect key could replace 'format'.
            if self.text(key).strip("'\"") != "format":
                continue
            selected = pair.child_by_field_name("value")
            if selected is not None:
                text = self.text(selected)
                explicit_formats.append(text in {"'csv'", '"csv"'} or
                                        text == "inputs.format" and self.inputs.get("format") == "csv")
        return explicit_formats == [True]

    def call(self, node: Node) -> _ToolValue | None:
        target = node.child_by_field_name("function")
        if target is None:
            return None
        args = node.child_by_field_name("arguments")
        if (field := self.field(target)) is not None:
            owner, _, name = field
            if owner.type == "identifier" and self.text(owner) == "tools" and name in self.schemas:
                return _ToolValue(name, self.schemas[name], csv=name == "read_data" and self.is_csv(args))
        if self.text(target) == "unwrap" and args is not None:
            values = [arg for arg in args.named_children if arg.type != "comment"]
            if len(values) == 1 and (value := self.values.get(values[0])) is not None and value.stage == "receipt":
                return replace(value, stage="value", receipt=values[0])
        return None

    def infer(self, node: Node) -> _ToolValue | None:
        if node.type == "identifier":
            return self.bindings.get(self.text(node))
        if node.type in {"await_expression", "parenthesized_expression"}:
            children = [child for child in node.named_children if child.type != "comment"]
            value = self.values.get(children[0]) if len(children) == 1 else None
            if value and node.type == "await_expression" and value.stage == "promise":
                return replace(value, stage="receipt")
            return value
        if node.type == "call_expression":
            return self.call(node)
        if (field := self.field(node)) is None:
            return None
        owner, _, name = field
        value = self.values.get(owner)
        if value is None or value.stage == "promise":
            return None
        if value.stage == "receipt":
            return replace(value, stage="value", receipt=owner) if name == "value" else None
        return None

    def edit(self, start: int, end: int, replacement: bytes) -> None:
        self.edits.append(_SourceEdit(start, end, replacement))

    def fix_receipt(self, value: _ToolValue, owner: Node, access: Node) -> bool:
        receipt, declaration = value.receipt, value.declaration
        if receipt is None:
            return False
        pair = access.parent
        key = pair.child_by_field_name("key") if pair is not None and pair.type == "pair" else None
        if key is None or self.text(key).strip("'\"") != "result_ref":
            return False
        if receipt.type == "identifier":
            handle = self.text(receipt)
            binding = self.bindings.get(handle)
            if binding is None or binding.stage != "receipt":
                return False
            if owner.type != "identifier":
                # Preserve the original unwrap check without issuing the tool again.
                self.edit(owner.start_byte, owner.end_byte, f"({self.text(owner)}, {handle})".encode())
                return True
        elif owner.type != "identifier" or declaration is None:
            return False
        elif declaration in self.receipt_bindings:
            handle = self.receipt_bindings[declaration]
        else:
            handle = f"$codeyReceipt{declaration.start_byte}"
            while handle.encode("ascii") in self.source:
                handle += "_"
            encoded = handle.encode("ascii")
            # Save the already awaited receipt. Keep the original initializer
            # around it, so any data uses still get exactly the same value.
            self.edit(declaration.start_byte, receipt.start_byte, encoded + b" = ")
            self.edit(receipt.end_byte, receipt.end_byte, b", " +
                      self.source[declaration.start_byte:receipt.start_byte] + encoded)
            self.receipt_bindings[declaration] = handle
        self.edit(owner.start_byte, owner.end_byte, handle.encode("utf-8"))
        return True

    def fix_rows(self, value: _ToolValue, owner: Node, key: Node) -> bool:
        if not value.csv or owner.type != "identifier" or (field := _csv_row_field(value.schema)) is None:
            return False
        if key.type == "string":
            replacement = json.dumps(field)
        elif field.isascii() and field.isidentifier():
            replacement = field
        else:
            return False
        self.edit(key.start_byte, key.end_byte, replacement.encode("utf-8"))
        if value.stage == "receipt":
            self.edit(owner.start_byte, owner.end_byte, f"unwrap({self.text(owner)})".encode())
        return True

    def access(self, node: Node) -> SourceDiagnostic | None:
        if (field := self.field(node)) is None:
            return None
        owner, key, name = field
        value = self.values.get(owner)
        if value is None or value.stage == "promise" or name in _OBJECT_PROPERTIES:
            return None
        variants = _variants(value.schema)
        if not all(_lacks_field(branch, name) for branch in variants):
            return None
        if not self.preflight:
            if value.stage != "value" or not all(branch.get("type") == "string" for branch in variants):
                return None
            return _diagnostic(key, self.source, "source_shape",
                f"{self.text(owner)} is a string according to the invoked tool's output schema; "
                f"property {name!r} is not its data. Use the string directly, or its standard string methods.")
        misplaced_ref = name == "result_ref" and value.stage == "value"
        missing_rows = name == "rows" and value.name == "read_data"
        if not (misplaced_ref or missing_rows):
            return None
        if self.rewrite and (self.fix_receipt(value, owner, node) if misplaced_ref else self.fix_rows(value, owner, key)):
            self.corrections += 1
            return None
        return _diagnostic(key, self.source, "source_shape", f"Invalid field {name!r} on the result of tools.{value.name}.")

    def statement(self, statement: Node) -> SourceDiagnostic | None:
        constant = statement.type == "lexical_declaration" and statement.children[0].type == "const"
        expressions = statement.named_children if constant else [statement]
        for expression in expressions:
            if (diagnostic := self.inspect(expression)) is not None:
                return diagnostic
            name = expression.child_by_field_name("name") if constant else None
            initializer = expression.child_by_field_name("value")
            if (name is not None and name.type == "identifier" and initializer is not None
                    and (value := self.values.get(initializer)) is not None):
                if value.receipt is not None and value.declaration is None:
                    value = replace(value, declaration=expression)
                self.bindings[self.text(name)] = value
        return None

    def inspect(self, expression: Node) -> SourceDiagnostic | None:
        # Walk in evaluation order so an unknown call or write cannot leave stale
        # evidence for a later field access in the same expression.
        self.values.clear()
        pending = [(expression, False)]
        while pending:
            node, visited = pending.pop()
            if visited:
                if (diagnostic := self.access(node)) is not None:
                    return diagnostic
                if (value := self.infer(node)) is not None:
                    self.values[node] = value
                elif node.type == "call_expression":
                    # Host tools receive JSON copies. An unknown JS call may mutate earlier values.
                    self.forget()
            elif node.type in _EXPRESSIONS:
                pending.append((node, True))
                pending.extend((child, False) for child in reversed(node.named_children))
            elif node.type in _MUTATION_BOUNDARIES:
                self.forget()
        return None

    def forget(self) -> None:
        self.bindings.clear()
        self.values.clear()

    def readonly_program(self, root: Node, catalog: ToolCatalogSnapshot) -> bool:
        readonly = {entry.name for entry in catalog.entries if entry.read_only}
        for node in _nodes(root):
            if node.type == "this":
                return False
            if node.type not in {"identifier", "shorthand_property_identifier"}:
                continue
            name = self.text(node)
            if name == "store" or "\\" in name:
                return False
            if name == "tools":
                owner = node.parent
                if owner is None or (field := self.field(owner)) is None or field[0] != node or field[2] not in readonly:
                    return False
        return True

    def rewritten_source(self) -> str:
        parts: list[bytes] = []
        previous = 0
        for edit in sorted(self.edits, key=lambda edit: (edit.start, edit.end)):
            if edit.start < previous:
                return ""
            parts.extend((self.source[previous:edit.start], edit.replacement))
            previous = edit.end
        parts.append(self.source[previous:])
        return b"".join(parts).decode("utf-8")


def _shadows_tool_api(root: Node, source: bytes) -> bool:
    pending = list(root.named_children)
    while pending:
        node = pending.pop()
        if node.type in {"variable_declarator", "function_declaration", "generator_function_declaration",
                         "class_declaration"}:
            name = node.child_by_field_name("name")
            if name is not None:
                for binding in _nodes(name):
                    if binding.type in {"identifier", "shorthand_property_identifier_pattern"}:
                        text = _node_text(binding, source)
                        if text in {"tools", "unwrap", "inputs"} or "\\" in text:
                            return True
        if node.type not in {"function_declaration", "generator_function_declaration", "function_expression",
                             "arrow_function", "method_definition"}:
            pending.extend(node.named_children)
    return False


def _check_shape(code: str, catalog: ToolCatalogSnapshot, *, preflight: bool,
                 inputs: Mapping[str, object], rewrite: bool) -> tuple[str, int, SourceDiagnostic | None]:
    source = code.encode("utf-8")
    root = Parser(Language(tree_sitter_javascript.language())).parse(source).root_node
    if root.has_error or _shadows_tool_api(root, source):
        return code, 0, None
    check = _ShapeCheck(source, catalog, preflight=preflight, inputs=inputs, rewrite=rewrite)
    if check.rewrite:
        check.rewrite = check.readonly_program(root, catalog)
    for statement in root.named_children:
        if (diagnostic := check.statement(statement)) is not None:
            return code, 0, diagnostic
        if statement.type in {"return_statement", "throw_statement"}:
            break
    if check.corrections and (rewritten := check.rewritten_source()):
        return rewritten, check.corrections, None
    return code, 0, None


def preflight_shape_diagnostic(code: str, catalog: ToolCatalogSnapshot) -> SourceDiagnostic | None:
    return _check_shape(code, catalog, preflight=True, inputs={}, rewrite=False)[2]


def shape_failure(diagnostic: SourceDiagnostic) -> str:
    return f"CodeMode line {diagnostic.line}, column {diagnostic.column}: {diagnostic.message}"


def preflight_plan(plan: ToolPlan, catalog: ToolCatalogSnapshot) -> ToolPlan:
    """Lower provable data/receipt accesses before the canonical call is admitted."""
    calls: list[ToolCall] = []
    corrections = 0
    for call in plan.calls:
        if call.name == "codemode" and not call.args.get("repair_of") and isinstance(code := call.args.get("code"), str):
            raw_inputs = call.args.get("inputs")
            inputs = raw_inputs if isinstance(raw_inputs, dict) else {}
            rewritten, count, diagnostic = _check_shape(code, catalog, preflight=True, inputs=inputs, rewrite=True)
            if diagnostic is not None:
                return ToolPlan([], None, protocol_error=shape_failure(diagnostic),
                                protocol_error_kind=diagnostic.code, protocol_tool_name=call.name)
            if count:
                args = {**call.args, "code": rewritten}
                try:
                    parse_codemode_args(args)
                except CodeModeInputError as exc:
                    return ToolPlan([], None, protocol_error=str(exc), protocol_error_kind=exc.code,
                                    protocol_tool_name=call.name)
                call = replace(call, args=args)
                corrections += count
        calls.append(call)
    if not corrections:
        return plan
    return replace(plan, calls=calls, arg_repair_counts={**plan.arg_repair_counts, "codemode_source_shape": corrections})


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
    diagnostic = _check_shape(code, catalog, preflight=False, inputs={}, rewrite=False)[2]
    if diagnostic is None:
        return ""
    guidance = ("Keep all original tool calls and arguments unchanged." if receipt_replay
                else "Continue from the settled child receipts; do not repeat completed actions.")
    return f"{diagnostic.message} {guidance}"
