"""Replace existing top-level JSON values without re-encoding other fields."""
from __future__ import annotations

import json
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import NoReturn

from codey.types.json_value import JsonValue

_WHITESPACE = re.compile(r"[ \t\r\n]*")
_NUMBER = re.compile(r"-?(?:0|[1-9][0-9]*)(?:\.[0-9]+)?(?:[eE][+-]?[0-9]+)?\Z")


@dataclass(frozen=True)
class JsonNumber:
    """A validated JSON number lexeme, never converted through binary floats."""
    text: str

    def __post_init__(self) -> None:
        if not self.text or len(self.text) > 1024 or _NUMBER.fullmatch(self.text) is None:
            raise ValueError("expected a bounded finite JSON number without units or separators")

    def __str__(self) -> str:
        return self.text

JSON_FIELD_EDIT_PROGRAM = (
    "const read = await tools.read_file({path: inputs.path});\n"
    "return unwrap(await tools.edit({read, changes: inputs.changes}));"
)

JSON_FIELD_EDIT_GUIDANCE = (
    "To persist changes to existing top-level JSON fields, use this CodeMode program. "
    "Put the requested file in inputs.path and the requested field values in inputs.changes.\n"
    + JSON_FIELD_EDIT_PROGRAM + "\n"
    "Pass read directly and unwrap only the edit result. Returning modified JSON alone does not write. "
    "If the file changed, rerun the same two lines to read the current file and repeat the edit."
)


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


def json_source_value(text: str, pointer: str) -> object:
    """Select from source JSON while retaining numeric lexemes and string types."""
    value: object = _DECODER.decode(text)
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
    original: object = _DECODER.decode(text)
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
