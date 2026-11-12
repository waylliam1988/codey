"""Replace existing top-level JSON values without re-encoding other fields."""
from __future__ import annotations

import json
import re
from typing import NoReturn

from codey.types.json_value import JsonObject

_WHITESPACE = re.compile(r"[ \t\r\n]*")

JSON_FIELD_EDIT_GUIDANCE = (
    "To persist changes to existing top-level JSON fields, use this CodeMode program. "
    "Put the requested file in inputs.path and the requested field values in inputs.changes.\n"
    "const read = await tools.read_file({path: inputs.path});\n"
    "return unwrap(await tools.edit({read, changes: inputs.changes}));\n"
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


_DECODER = json.JSONDecoder(object_pairs_hook=_unique, parse_int=str, parse_float=str, parse_constant=_invalid_constant)


def _skip_whitespace(text: str, offset: int) -> int:
    match = _WHITESPACE.match(text, offset)
    assert match is not None
    return match.end()


def replace_json_fields(text: str, changes: JsonObject) -> str:
    if not changes:
        raise ValueError("changes must name at least one existing top-level JSON field")
    original: object = _DECODER.decode(text)
    if not isinstance(original, dict):
        raise ValueError("the file must contain a JSON object")
    missing = changes.keys() - original.keys()
    if missing:
        raise ValueError(f"fields do not exist in the original JSON object: {', '.join(sorted(missing))}")

    cursor = _skip_whitespace(text, 0) + 1
    copied = 0
    parts: list[str] = []
    # The decoder has already validated the document. Walk the original spans
    # so unchanged text and numeric precision survive the update.
    for key in original:
        _, key_end = _DECODER.raw_decode(text, _skip_whitespace(text, cursor))
        start = _skip_whitespace(text, _skip_whitespace(text, key_end) + 1)
        _, end = _DECODER.raw_decode(text, start)
        if key in changes:
            parts.extend((text[copied:start], json.dumps(changes[key], allow_nan=False)))
            copied = end
        cursor = _skip_whitespace(text, end) + 1
    parts.append(text[copied:])
    return "".join(parts)
