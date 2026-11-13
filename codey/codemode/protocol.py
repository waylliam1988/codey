from __future__ import annotations

import json
import re
from collections.abc import Mapping

from codey.codemode.types import CodeModeLimits, CodeModeProgram
from codey.runtime.core.models import canonical_json_mapping
from codey.types.json_value import JsonValue, parse_json_object

_DENIED_SOURCE = re.compile(
    r"(?:\bprocess\b|\brequire\b|\bimport\b|\bfetch\b|\beval\b|"
    r"\bglobalThis\b|\b__dirname\b|\b__filename\b|\bchild_process\b|\bfs\b|\bconstructor\b|"
    r"tools\s*(?:\.\s*codemode|\[\s*['\"]codemode['\"]\s*\])\b)",
    re.IGNORECASE,
)
_REPAIR_TOKEN = re.compile(r"cmr_[A-Za-z0-9_-]{6,64}\Z")
_COMMENTED_STATEMENT = re.compile(r"\\(?:r\\n|n|r)[ \t]*(?:const|let|var|await|return|throw)\b")


class CodeModeInputError(ValueError):
    def __init__(self, message: str, *, code: str = "invalid_args") -> None:
        super().__init__(message)
        self.code = code


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
    import tree_sitter_javascript
    from tree_sitter import Language, Parser

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
        lines = re.split(r"\r\n|[\n\r\u2028\u2029]", prefix)
        raise CodeModeInputError(
            f"codemode rejected before execution at line {len(lines)}, column {len(lines[-1]) + 1}: "
            "a literal backslash newline escape inside a // comment leaves the following "
            f"'{match.group(0)}' text commented out. No child action was executed. "
            "If it is intended code, use an actual JavaScript line break after JSON decoding "
            "or remove the single-line comment. If it is a commented example, use a /* block comment */. "
            "The source was not rewritten.",
            code="commented_statement",
        )


def parse_codemode_args(args: Mapping[str, object], *, limits: CodeModeLimits | None = None) -> CodeModeProgram:
    if not isinstance(args, Mapping):
        raise CodeModeInputError("codemode args must be an object")
    active_limits = limits or CodeModeLimits()
    code = args.get("code")
    if not isinstance(code, str) or not code.strip():
        raise CodeModeInputError("codemode code must be a non-empty string")
    try:
        inputs = canonical_json_mapping(args.get("inputs", {}))
        _check_input_numbers(inputs)
        input_bytes = len(json.dumps(inputs, ensure_ascii=False, separators=(",", ":"),
                                    allow_nan=False).encode("utf-8")) if inputs else 0
        source_bytes = len(code.encode("utf-8"))
    except (TypeError, ValueError, RecursionError) as exc:
        raise CodeModeInputError(f"codemode inputs must be a strict JSON object: {exc}") from exc
    if source_bytes + input_bytes > active_limits.source_bytes:
        raise CodeModeInputError("codemode source and inputs exceed source byte limit", code="source_limit")
    language = str(args.get("language", "javascript") or "javascript").strip().lower()
    if language not in {"javascript", "js"}:
        raise CodeModeInputError("codemode language must be javascript")
    denied = _DENIED_SOURCE.search(code) or re.search(r"\bFunction\s*\(", code)
    if denied:
        access = ("recursive invocation" if denied.group(0).lower().startswith("tools")
                  else f"direct host access token '{denied.group(0)}'")
        raise CodeModeInputError(
            f"codemode rejected before execution: {access} is denied.", code="sandbox_denied"
        )
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
    except json.JSONDecodeError as exc:
        if exc.msg == "Extra data":
            raise ValueError("codemode text must contain exactly one JSON object") from exc
        raise ValueError(f"codemode text is not valid JSON: {exc.msg}") from exc
    if not isinstance(decoded, dict) or set(decoded) != {"tool", "args"}:
        raise ValueError("codemode text must contain exactly one tool and args object")
    if decoded.get("tool") != "codemode":
        raise ValueError("codemode text must name the codemode tool")
    args = decoded.get("args")
    if not isinstance(args, dict):
        raise ValueError("codemode args must be an object")
    return parse_codemode_args(args, limits=limits)
