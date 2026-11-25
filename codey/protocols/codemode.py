from __future__ import annotations

import json
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from types import MappingProxyType

import tree_sitter_javascript
from tree_sitter import Language, Node, Parser

from codey.types.json_value import JsonObject, JsonValue, canonical_json_object, parse_json_object


def _freeze_value(value: JsonValue) -> object:
    if isinstance(value, dict):
        return MappingProxyType({key: _freeze_value(item) for key, item in value.items()})
    if isinstance(value, list):
        return tuple(_freeze_value(item) for item in value)
    return value


def freeze_codemode_mapping(value: Mapping[str, object]) -> Mapping[str, object]:
    return MappingProxyType({key: _freeze_value(item) for key, item in canonical_json_object(value).items()})


@dataclass(frozen=True)
class CodeModeLimits:
    """One attempt's worker limits; child positions span the logical invocation.

    Replayed positions consume their original receipts, not new effect budget.
    timeout_ms bounds each worker attempt and inherits any outer deadline.
    """
    source_bytes: int = 100_000
    timeout_ms: int = 30_000
    max_child_calls: int = 64
    max_concurrent_calls: int = 8
    max_output_bytes: int = 200_000
    max_pending_promises: int = 64
    max_rpc_bytes: int = 1_000_000

    def as_dict(self) -> dict[str, int]:
        return {
            "source_bytes": self.source_bytes,
            "timeout_ms": self.timeout_ms,
            "max_child_calls": self.max_child_calls,
            "max_concurrent_calls": self.max_concurrent_calls,
            "max_output_bytes": self.max_output_bytes,
            "max_pending_promises": self.max_pending_promises,
            "max_rpc_bytes": self.max_rpc_bytes,
        }

    def __post_init__(self) -> None:
        for field_name, value in self.as_dict().items():
            if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
                raise ValueError(f"{field_name} must be a positive integer")
        if self.max_concurrent_calls > self.max_child_calls:
            raise ValueError("max_concurrent_calls cannot exceed max_child_calls")


@dataclass(frozen=True)
class CodeModeProgram:
    code: str
    language: str = "javascript"
    inputs: Mapping[str, object] = field(default_factory=dict)
    repair_of: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "inputs", freeze_codemode_mapping(self.inputs))
        object.__setattr__(self, "repair_of", str(self.repair_of or "").strip())


_REPAIR_TOKEN = re.compile(r"cmr_[A-Za-z0-9_-]{6,64}\Z")
_COMMENTED_STATEMENT = re.compile(r"\\(?:r\\n|n|r)[ \t]*(?:const|let|var|await|return|throw)\b")


class CodeModeInputError(ValueError):
    def __init__(self, message: str, *, code: str = "invalid_args") -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class SourceDiagnostic:
    code: str
    message: str
    line: int
    column: int


_DENIED_IDENTIFIERS = frozenset({
    "process", "globalThis", "fetch", "__dirname", "__filename", "child_process", "fs", "require", "eval", "Function",
})


def _node_text(node: Node, source: bytes) -> str:
    return source[node.start_byte:node.end_byte].decode("utf-8")


def _source_location(prefix: str) -> tuple[int, int]:
    lines = re.split(r"\r\n|[\n\r\u2028\u2029]", prefix)
    return len(lines), len(lines[-1]) + 1


def _diagnostic(node: Node, source: bytes, code: str, message: str) -> SourceDiagnostic:
    line, column = _source_location(source[:node.start_byte].decode("utf-8"))
    return SourceDiagnostic(code, message, line, column)


def _is_property_name(node: Node) -> bool:
    parent = node.parent
    if parent is None:
        return False
    if node.type == "property_identifier":
        return parent.type in {"pair", "member_expression", "method_definition", "public_field_definition"}
    return node.type == "string" and parent.type in {"pair", "member_expression", "subscript_expression"}


def _recursive_codemode_call(node: Node, source: bytes) -> bool:
    if node.type == "member_expression":
        object_node = node.child_by_field_name("object")
        property_node = node.child_by_field_name("property")
        return (object_node is not None and property_node is not None
                and _node_text(object_node, source) == "tools"
                and _node_text(property_node, source) == "codemode")
    if node.type == "subscript_expression":
        object_node = node.child_by_field_name("object")
        index_node = node.child_by_field_name("index")
        return (object_node is not None and index_node is not None
                and _node_text(object_node, source) == "tools"
                and _node_text(index_node, source).strip("'\"") == "codemode")
    return False


def _is_call_target(node: Node) -> bool:
    parent = node.parent
    return parent is not None and parent.type == "call_expression" and parent.child_by_field_name("function") == node


def analyze_codemode_source(source: str) -> tuple[SourceDiagnostic, ...]:
    """Return syntax-aware host-access diagnostics for one CodeMode program."""
    encoded = source.encode("utf-8")
    root = Parser(Language(tree_sitter_javascript.language())).parse(encoded).root_node
    diagnostics: list[SourceDiagnostic] = []
    stack = [root]
    while stack:
        node = stack.pop()
        if node.type == "import_statement" or node.type == "import":
            diagnostics.append(_diagnostic(node, encoded, "sandbox_denied", "module imports are not available in CodeMode"))
        elif node.type == "call_expression":
            function = node.child_by_field_name("function")
            if function is not None:
                name = _node_text(function, encoded)
                if name in {"require", "eval", "Function", "fetch"}:
                    diagnostics.append(_diagnostic(node, encoded, "sandbox_denied", f"direct host access '{name}' is denied"))
                elif _recursive_codemode_call(function, encoded):
                    diagnostics.append(_diagnostic(node, encoded, "sandbox_denied", "recursive codemode invocation is denied"))
                elif function.type == "member_expression":
                    property_node = function.child_by_field_name("property")
                    if property_node is not None and _node_text(property_node, encoded) == "constructor":
                        diagnostics.append(_diagnostic(node, encoded, "sandbox_denied", "constructor-based code generation is denied"))
        elif node.type == "new_expression":
            function = node.child_by_field_name("constructor") or node.child_by_field_name("function")
            if function is not None and _node_text(function, encoded) == "Function":
                diagnostics.append(_diagnostic(node, encoded, "sandbox_denied", "direct host access 'Function' is denied"))
        elif (node.type in {"identifier", "shorthand_property_identifier"}
              and _node_text(node, encoded) in _DENIED_IDENTIFIERS
              and not _is_property_name(node) and not _is_call_target(node)):
            diagnostics.append(_diagnostic(node, encoded, "sandbox_denied", f"direct host access '{_node_text(node, encoded)}' is denied"))
        stack.extend(reversed(node.named_children))
    return tuple(diagnostics)


def _raise_source_diagnostic(diagnostic: SourceDiagnostic) -> None:
    raise CodeModeInputError(
        f"codemode rejected before execution at line {diagnostic.line}, column {diagnostic.column}: "
        f"{diagnostic.message}.", code=diagnostic.code,
    )


def _check_input_numbers(value: JsonValue) -> None:
    if type(value) is int and abs(value) > 9_007_199_254_740_991:
        raise CodeModeInputError("codemode inputs integers must be JavaScript safe integers")
    if isinstance(value, dict):
        for item in value.values():
            _check_input_numbers(item)
    elif isinstance(value, list):
        for item in value:
            _check_input_numbers(item)


def _check_commented_statements(code: str) -> None:
    """Diagnose likely double-escaped newlines, without rewriting user source."""
    if not _COMMENTED_STATEMENT.search(code):
        return
    source = code.encode("utf-8")
    pending = [Parser(Language(tree_sitter_javascript.language())).parse(source).root_node]
    while pending:
        node = pending.pop()
        if node.type != "comment":
            pending.extend(reversed(node.named_children))
            continue
        comment = source[node.start_byte:node.end_byte].decode("utf-8")
        if not comment.startswith("//"):
            continue
        match = _COMMENTED_STATEMENT.search(comment)
        if match is None:
            continue
        prefix = source[:node.start_byte].decode("utf-8") + comment[:match.start()]
        # JavaScript also treats CR and Unicode separators as line terminators.
        line, column = _source_location(prefix)
        raise CodeModeInputError(
            f"codemode rejected before execution at line {line}, column {column}: "
            "a literal backslash newline escape inside a // comment leaves the following "
            f"'{match.group(0)}' text commented out. No child action was executed. "
            "If it is intended code, use an actual JavaScript line break after JSON decoding "
            "or remove the single-line comment. If it is a commented example, use a /* block comment */. "
            "The source was not rewritten.",
            code="commented_statement",
        )


def codemode_parameters() -> tuple[tuple[str, JsonObject], ...]:
    return (
        ("code", {"type": "string"}),
        ("inputs", {"type": "object", "additionalProperties": True,
                    "description": "Named JSON data fields exposed as read-only inputs in JavaScript."}),
        ("language", {"type": "string", "enum": ["javascript", "js"]}),
        ("repair_of", {"type": "string", "pattern": "^cmr_[A-Za-z0-9_-]{6,64}$",
                       "description": "Optional kernel-issued token for a return-only receipt replay."}),
    )


def parse_codemode_args(args: Mapping[str, object], *, limits: CodeModeLimits | None = None) -> CodeModeProgram:
    if not isinstance(args, Mapping):
        raise CodeModeInputError("codemode args must be an object")
    if args.keys() - {name for name, _ in codemode_parameters()}:
        raise CodeModeInputError("unsupported CodeMode arguments")
    active_limits = limits or CodeModeLimits()
    code = args.get("code")
    if not isinstance(code, str) or not code.strip():
        raise CodeModeInputError("codemode code must be a non-empty string")
    try:
        inputs = canonical_json_object(args.get("inputs", {}))
        _check_input_numbers(inputs)
        input_bytes = len(json.dumps(inputs, ensure_ascii=False, separators=(",", ":"),
                                    allow_nan=False).encode("utf-8")) if inputs else 0
    except (TypeError, ValueError, RecursionError) as exc:
        raise CodeModeInputError(f"codemode inputs must be a strict JSON object: {exc}") from exc
    try:
        source_bytes = len(code.encode("utf-8"))
    except UnicodeEncodeError as exc:
        raise CodeModeInputError("codemode code must contain valid Unicode") from exc
    if source_bytes + input_bytes > active_limits.source_bytes:
        raise CodeModeInputError("codemode source and inputs exceed source byte limit", code="source_limit")
    language = str(args.get("language", "javascript") or "javascript").strip().lower()
    if language not in {"javascript", "js"}:
        raise CodeModeInputError("codemode language must be javascript")
    diagnostics = analyze_codemode_source(code)
    if diagnostics:
        _raise_source_diagnostic(diagnostics[0])
    repair_of = args.get("repair_of", "")
    if repair_of is None:
        repair_of = ""
    if not isinstance(repair_of, str) or (repair_of and not _REPAIR_TOKEN.fullmatch(repair_of)):
        raise CodeModeInputError("codemode repair_of must be a valid repair token", code="invalid_repair")
    _check_commented_statements(code)
    return CodeModeProgram(code=code, language=language, inputs=inputs, repair_of=repair_of)


def parse_codemode_text(text: str, *, limits: CodeModeLimits | None = None) -> CodeModeProgram:
    if not isinstance(text, str):
        raise ValueError("codemode text must be a string")
    try:
        decoded = parse_json_object(text)
    except RecursionError as exc:
        raise ValueError("codemode text exceeds the JSON nesting limit") from exc
    except json.JSONDecodeError as exc:
        if exc.msg == "Extra data":
            raise ValueError("codemode text must contain exactly one JSON object") from exc
        raise ValueError(f"codemode text is not valid JSON: {exc.msg}") from exc
    if set(decoded) != {"tool", "args"}:
        raise ValueError("codemode text must contain exactly one tool and args object")
    if decoded.get("tool") != "codemode":
        raise ValueError("codemode text must name the codemode tool")
    args = decoded.get("args")
    if not isinstance(args, dict):
        raise ValueError("codemode args must be an object")
    return parse_codemode_args(args, limits=limits)
