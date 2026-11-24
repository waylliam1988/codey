"""Replace existing top-level JSON values without re-encoding other fields."""
from __future__ import annotations

import json
import re
from collections.abc import Collection, Iterable, Mapping
from dataclasses import dataclass
from typing import NoReturn

from codey.types.json_value import JsonValue

_WHITESPACE = re.compile(r"[ \t\r\n]*")
_NUMBER = re.compile(r"-?(?:0|[1-9][0-9]*)(?:\.[0-9]+)?(?:[eE][+-]?[0-9]+)?\Z")
_JSON_START = re.compile(r"[\[{]")


@dataclass(frozen=True)
class JsonNumber:
    """A validated JSON number lexeme, never converted through binary floats."""
    text: str

    def __post_init__(self) -> None:
        if not self.text or len(self.text) > 1024 or _NUMBER.fullmatch(self.text) is None:
            raise ValueError("expected a bounded finite JSON number without units or separators")

    def __str__(self) -> str:
        return self.text

    def numeric_key(self) -> tuple[bool, str, int]:
        """Compare finite decimal values without a float or Decimal context limit."""
        mantissa, _, exponent = self.text.lower().partition("e")
        whole, _, fractional = mantissa.lstrip("-").partition(".")
        digits = (whole + fractional).lstrip("0")
        if not digits:
            return False, "0", 0
        significant = digits.rstrip("0")
        scale = int(exponent or "0") - len(fractional) + len(digits) - len(significant)
        return mantissa.startswith("-"), significant, scale

JSON_FIELD_EDIT_PROGRAM = (
    "const read = await tools.read_file({path: inputs.path});\n"
    "return unwrap(await tools.edit({base_result_ref: read.result_ref, changes: inputs.changes}));"
)

JSON_FIELD_EDIT_GUIDANCE = (
    "To persist changes to existing top-level JSON fields, use this CodeMode program. "
    "Put the requested file in inputs.path and the requested field values in inputs.changes.\n"
    + JSON_FIELD_EDIT_PROGRAM + "\n"
    "Pass read.result_ref as base_result_ref and unwrap only the edit result. Returning modified JSON alone does not write. "
    "If the file changed, rerun the same two lines to read the current file and repeat the edit."
)

CONTENT_EDIT_GUIDANCE = (
    "The task requires a whole-file edit using content and the outer read Result.result_ref. "
    "Read the complete current file, construct content from that read preserving all unrelated fields, "
    "then call tools.edit({path:inputs.path,base_result_ref:read.result_ref,content}). "
    "On a version conflict, read again and reconstruct content from the new file; do not reuse stale content. "
    "Do not substitute the read+changes field-edit form for this declared whole-file edit."
)

DECLARED_FIELD_EDIT_GUIDANCE = (
    "Copy task-declared fields directly from their committed browser receipts without transcribing values. "
    "Put the requested destination in inputs.path and use the original outer read Result.result_ref:\n"
    "const read=await tools.read_file({path:inputs.path});unwrap(read);"
    "return unwrap(await tools.edit({path:inputs.path,base_result_ref:read.result_ref,declared_fields:true}));\n"
    "Do not supply changes, replacements or manually copied values. "
    "On a version conflict, obtain a new complete file read and repeat this declared-field edit."
)


def edit_guidance(required: Collection[str], parameters: Collection[str]) -> str:
    """Select the edit template from the same frozen input contract used to execute."""
    if "content" in required:
        return CONTENT_EDIT_GUIDANCE
    if "declared_fields" in parameters:
        return DECLARED_FIELD_EDIT_GUIDANCE
    return JSON_FIELD_EDIT_GUIDANCE


def requires_content_edit(task: str) -> bool:
    return re.search(r"(?:使用|用|with|using)\s*`?content`?\s*(?:写入|写回|write|for)", task, re.I) is not None


def _unique(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON field: {key!r}")
        result[key] = value
    return result


def _invalid_constant(value: str) -> NoReturn:
    raise ValueError(f"invalid JSON constant: {value}")


_DECODER = json.JSONDecoder(object_pairs_hook=_unique, parse_int=JsonNumber, parse_float=JsonNumber,
                            parse_constant=_invalid_constant)


def _decode_document(text: str) -> object:
    try:
        decoded: object = _DECODER.decode(text)
    except RecursionError as exc:
        raise ValueError("JSON nesting exceeds the decoder limit") from exc
    return decoded


def last_json_object(text: str) -> str | None:
    """Locate complete outer objects in document order without rounding numbers."""
    content = None
    cursor = 0
    while (match := _JSON_START.search(text, cursor)) is not None:
        start = match.start()
        try:
            span: tuple[object, int] = _DECODER.raw_decode(text, start)
        except ValueError:
            cursor = start + 1
            continue
        value, cursor = span
        if isinstance(value, dict):
            content = text[start:cursor]
    return content


def json_source_value(text: str, pointer: str) -> object:
    """Select from source JSON while retaining numeric lexemes and string types."""
    value = _decode_document(text)
    if not pointer.startswith("/"):
        raise ValueError("source selector must be a JSON pointer")
    for encoded in pointer[1:].split("/"):
        if re.search(r"~(?![01])", encoded):
            raise ValueError("invalid JSON pointer escape")
        key = encoded.replace("~1", "/").replace("~0", "~")
        if isinstance(value, dict):
            values = dict[str, object](value)
            value = values[key]
        elif isinstance(value, list) and re.fullmatch(r"0|[1-9][0-9]*", key):
            items = list[object](value)
            value = items[int(key)]
        else:
            raise ValueError("source pointer does not select a value")
    return value


def _skip_whitespace(text: str, offset: int) -> int:
    match = _WHITESPACE.match(text, offset)
    assert match is not None
    return match.end()


def _field_names(fields: Iterable[str]) -> str:
    names = sorted(fields)
    return json.dumps(names[:12], ensure_ascii=False) + (f" ({len(names)} total)" if len(names) > 12 else "")


def replace_json_fields(text: str, changes: Mapping[str, JsonValue | JsonNumber]) -> str:
    if not changes:
        raise ValueError("changes must name at least one existing top-level JSON field")
    original = _decode_document(text)
    if not isinstance(original, dict):
        raise ValueError("the file must contain a JSON object")
    document: dict[str, object] = original
    missing = changes.keys() - document.keys()
    if missing:
        message = (f"fields do not exist in the original JSON object: {_field_names(missing)}. "
                   f"Existing top-level fields: {_field_names(document)}. Submitted fields: {_field_names(changes)}.")
        for key in sorted(missing):
            nested = changes[key]
            if isinstance(nested, dict) and nested and nested.keys() <= document.keys():
                message += (f" changes[{json.dumps(key, ensure_ascii=False)}] wraps existing fields "
                            f"{_field_names(nested)}; check whether this is an extra object layer.")
                break
        raise ValueError(message)

    cursor = _skip_whitespace(text, 0) + 1
    copied = 0
    parts: list[str] = []
    # The decoder has already validated the document. Walk the original spans
    # so unchanged text and numeric precision survive the update.
    for key in document:
        key_span: tuple[object, int] = _DECODER.raw_decode(text, _skip_whitespace(text, cursor))
        key_end = key_span[1]
        start = _skip_whitespace(text, _skip_whitespace(text, key_end) + 1)
        value_span: tuple[object, int] = _DECODER.raw_decode(text, start)
        end = value_span[1]
        if key in changes:
            value = changes[key]
            encoded = value.text if isinstance(value, JsonNumber) else json.dumps(value, allow_nan=False)
            parts.extend((text[copied:start], encoded))
            copied = end
        cursor = _skip_whitespace(text, end) + 1
    parts.append(text[copied:])
    return "".join(parts)
