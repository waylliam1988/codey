"""Canonical persisted argument shapes for safe tool replay."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

from codey.runtime.replay_policy import is_replayable_safe_tool
from codey.tool_args_repair import ToolArgLimits

REPLAY_ARG_TEXT_MAX_CHARS = 1000
REPLAY_READ_MAX_LINES = ToolArgLimits().read_max_lines
_REPLAY_READ_MAX_OFFSET = 1_000_000_000


@dataclass(frozen=True)
class ReplayArgSpec:
    allowed: frozenset[str]
    required: frozenset[str]


_REPLAY_ARG_SPECS = {
    "read": ReplayArgSpec(
        allowed=frozenset({"path", "offset", "limit"}),
        required=frozenset({"path"}),
    ),
    "ls": ReplayArgSpec(
        allowed=frozenset({"path"}),
        required=frozenset({"path"}),
    ),
    "search": ReplayArgSpec(
        allowed=frozenset({"path", "query"}),
        required=frozenset({"path", "query"}),
    ),
    "references": ReplayArgSpec(
        allowed=frozenset({"path", "symbol"}),
        required=frozenset({"path", "symbol"}),
    ),
}
REPLAY_ARG_TOOL_NAMES = frozenset(_REPLAY_ARG_SPECS)


def _validate_replay_text_arg(value: object, key: str) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{key} must be a string")
    if not value.strip():
        raise ValueError(f"{key} cannot be empty")
    if len(value) > REPLAY_ARG_TEXT_MAX_CHARS:
        raise ValueError(f"{key} exceeds max length {REPLAY_ARG_TEXT_MAX_CHARS}")
    return value


def _validate_replay_path(value: object) -> str:
    path = _validate_replay_text_arg(value, "path")
    if path.strip() != path:
        raise ValueError("path must already be canonical")
    if "\\" in path or path.startswith("/") or path.startswith("//"):
        raise ValueError("path must be project-relative and canonical")
    if len(path) >= 2 and path[1] == ":" and path[0].isalpha():
        raise ValueError("path must be project-relative and canonical")
    if path != ".":
        parts = path.split("/")
        if any(part in {"", ".", ".."} for part in parts):
            raise ValueError("path must already be canonical")
    return path


def _validate_replay_positive_int(value: object, key: str, maximum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{key} must be a positive integer")
    if value < 1:
        raise ValueError(f"{key} must be a positive integer")
    if value > maximum:
        raise ValueError(f"{key} exceeds max value {maximum}")
    return value


def validate_replay_args_shape(
    tool_name: str,
    args: Mapping[str, object],
) -> dict[str, object]:
    """Validate persisted replay args without aliases, repairs, or defaults."""
    canonical_name = str(tool_name or "").strip()
    if not is_replayable_safe_tool(canonical_name):
        raise ValueError(f"tool is not replayable: {canonical_name}")
    if not isinstance(args, Mapping) or isinstance(args, bool):
        raise ValueError("replay args must be a mapping")

    keys: set[str] = set()
    for key in args:
        if not isinstance(key, str) or not key:
            raise ValueError(f"invalid replay arg key: {key}")
        keys.add(key)

    spec = _REPLAY_ARG_SPECS.get(canonical_name)
    if spec is None:
        raise ValueError(f"tool has no replay arg schema: {canonical_name}")
    unknown = sorted(keys - spec.allowed)
    if unknown:
        raise ValueError(f"unsupported replay arg fields: {', '.join(unknown)}")
    missing = sorted(spec.required - keys)
    if missing:
        raise ValueError(f"missing replay arg fields: {', '.join(missing)}")

    validated: dict[str, object] = {"path": _validate_replay_path(args["path"])}
    if canonical_name == "read":
        if "offset" in args:
            validated["offset"] = _validate_replay_positive_int(
                args["offset"],
                "offset",
                _REPLAY_READ_MAX_OFFSET,
            )
        if "limit" in args:
            validated["limit"] = _validate_replay_positive_int(
                args["limit"],
                "limit",
                REPLAY_READ_MAX_LINES,
            )
    elif canonical_name == "search":
        validated["query"] = _validate_replay_text_arg(args["query"], "query")
    elif canonical_name == "references":
        validated["symbol"] = _validate_replay_text_arg(args["symbol"], "symbol")
    return validated


__all__ = [
    "REPLAY_ARG_TOOL_NAMES",
    "REPLAY_ARG_TEXT_MAX_CHARS",
    "REPLAY_READ_MAX_LINES",
    "validate_replay_args_shape",
]
