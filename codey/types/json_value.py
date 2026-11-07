"""Detached strict JSON values, separate from Python transport serialization.

No display clipping, default=str conversion, or integer coercion belongs here.
Container cycles and oversized/deep values fail before reaching core logic.
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import dataclass, fields, is_dataclass
from enum import Enum
from typing import TypeAlias

JsonPrimitive: TypeAlias = None | bool | int | float | str
JsonValue: TypeAlias = JsonPrimitive | list["JsonValue"] | dict[str, "JsonValue"]
JsonObject: TypeAlias = dict[str, JsonValue]
ToolArgs: TypeAlias = JsonObject


@dataclass(frozen=True)
class JsonLimits:
    max_depth: int = 64
    max_nodes: int = 1_000_000
    max_string_bytes: int = 32 * 1024 * 1024

    def __post_init__(self) -> None:
        if any(type(value) is not int or value < 1 for value in (
            self.max_depth, self.max_nodes, self.max_string_bytes,
        )):
            raise ValueError("JSON limits must be positive integers")


@dataclass
class _Decoder:
    limits: JsonLimits
    active: set[int]
    nodes: int = 0
    string_bytes: int = 0

    def string(self, value: str) -> str:
        try:
            self.string_bytes += len(value.encode("utf-8"))
        except UnicodeEncodeError as exc:
            raise ValueError("JSON string must contain valid Unicode") from exc
        if self.string_bytes > self.limits.max_string_bytes:
            raise ValueError("JSON string byte budget exceeded")
        return value

    def value(self, value: object, depth: int, *, transport: bool, tuples: bool) -> JsonValue:
        self.nodes += 1
        if depth > self.limits.max_depth or self.nodes > self.limits.max_nodes:
            raise ValueError("JSON depth or node budget exceeded")
        if transport and isinstance(value, Enum):
            return self.value(value.value, depth + 1, transport=transport, tuples=tuples)
        if value is None:
            return None
        if isinstance(value, str) and type(value) is str:
            return self.string(value)
        if isinstance(value, (bool, int)) and type(value) in (bool, int):
            return value
        if isinstance(value, float) and type(value) is float and math.isfinite(value):
            return value
        identity = id(value)
        if identity in self.active:
            raise ValueError("JSON container cycle")
        self.active.add(identity)
        try:
            return self.container(value, depth, transport=transport, tuples=tuples)
        finally:
            self.active.remove(identity)

    def container(self, value: object, depth: int, *, transport: bool, tuples: bool) -> JsonValue:
        if isinstance(value, Mapping):
            result: JsonObject = {}
            for key, item in value.items():
                if not isinstance(key, str) or type(key) is not str:
                    raise ValueError("JSON object keys must be strings")
                result[self.string(key)] = self.value(item, depth + 1, transport=transport, tuples=tuples)
            return result
        if isinstance(value, list) or (tuples and isinstance(value, tuple)):
            return [self.value(item, depth + 1, transport=transport, tuples=tuples) for item in value]
        if transport and isinstance(value, (set, frozenset)):
            return [self.value(item, depth + 1, transport=True, tuples=True) for item in sorted(value)]
        if transport and is_dataclass(value) and not isinstance(value, type):
            return {self.string(field.name): self.value(getattr(value, field.name), depth + 1,
                    transport=True, tuples=True) for field in fields(value)}
        raise ValueError(f"JSON cannot encode {type(value).__name__}")


DEFAULT_JSON_LIMITS = JsonLimits()


def decode_json_value(value: object, *, limits: JsonLimits = DEFAULT_JSON_LIMITS) -> JsonValue:
    """Validate and detach external JSON; Python tuple/set/dataclass are rejected."""
    return _Decoder(limits, set()).value(value, 0, transport=False, tuples=False)


def decode_json_object(value: object, *, limits: JsonLimits = DEFAULT_JSON_LIMITS) -> JsonObject:
    decoded = decode_json_value(value, limits=limits)
    if not isinstance(decoded, dict):
        raise ValueError("JSON value must be an object")
    return decoded


def canonical_json_object(value: object, *, limits: JsonLimits = DEFAULT_JSON_LIMITS) -> JsonObject:
    """Detach canonical internal payloads, preserving legacy tuple-to-list support."""
    decoded = _Decoder(limits, set()).value(value, 0, transport=False, tuples=True)
    if not isinstance(decoded, dict):
        raise ValueError("canonical payload must be a mapping")
    return decoded


def encode_transport_value(value: object, *, limits: JsonLimits = DEFAULT_JSON_LIMITS) -> JsonValue:
    """Encode explicitly supported Python transport structures into strict JSON."""
    return _Decoder(limits, set()).value(value, 0, transport=True, tuples=True)


def require_json_string(value: object, field: str) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{field} must be a string")
    return value


def require_json_integer(value: object, field: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool):
        raise ValueError(f"{field} must be an integer")
    return value


def require_json_boolean(value: object, field: str) -> bool:
    if not isinstance(value, bool):
        raise ValueError(f"{field} must be a boolean")
    return value


def require_json_strings(value: object, field: str) -> list[str]:
    if not isinstance(value, list):
        raise ValueError(f"{field} must be an array of strings")
    return [require_json_string(item, field) for item in value]
