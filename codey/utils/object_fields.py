"""Validate dynamic object keys before typed JSON field access."""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from typing import TypeGuard


def object_fields(value: object) -> TypeGuard[dict[str, object]]:
    if not isinstance(value, dict):
        return False
    fields: Mapping[object, object] = value
    return all(isinstance(key, str) for key in fields)


def mapping_fields(value: object) -> TypeGuard[Mapping[str, object]]:
    if not isinstance(value, Mapping):
        return False
    fields: Mapping[object, object] = value
    return all(isinstance(key, str) for key in fields)


def sequence_items(value: object) -> TypeGuard[Sequence[object]]:
    return isinstance(value, (list, tuple))


def require_object_fields(value: object) -> dict[str, object]:
    if not object_fields(value):
        raise TypeError("control data must be an object with string keys")
    return value


def require_object_rows(value: object) -> list[dict[str, object]]:
    if not sequence_items(value):
        raise TypeError("control records must be a sequence")
    return [require_object_fields(row) for row in value]


def decoded_object(encoded: object) -> dict[str, object]:
    if not isinstance(encoded, str):
        raise ValueError("JSON payload must be encoded text")
    value: object = json.loads(encoded)
    if not object_fields(value):
        raise ValueError("JSON payload must be an object with string keys")
    return value
