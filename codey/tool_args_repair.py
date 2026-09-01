"""Shared tool argument canonicalization and repair.

Pure functions only. No dependencies on providers, agents, runtime executors,
task runners, or ghost.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any


_DRIVE_PATH_RE = re.compile(r"^[A-Za-z]:")


@dataclass(frozen=True)
class ToolArgLimits:
    max_replacements: int = 8
    read_default_lines: int = 300
    read_max_lines: int = 600


@dataclass(frozen=True)
class ToolArgsRepairResult:
    args: dict[str, Any]
    alias_rewrite_count: int = 0
    arg_repair_counts: dict[str, int] = field(default_factory=dict)


class ToolArgsRepairError(ValueError):
    """Raised when tool arguments are invalid and cannot be safely repaired."""

    def __init__(self, message: str, *, repair_kind: str = "invalid_args") -> None:
        super().__init__(message)
        self.repair_kind = repair_kind


def _record_repair(counts: dict[str, int], kind: str) -> None:
    counts[kind] = counts.get(kind, 0) + 1


def _normalize_path(
    raw_value: object,
    *,
    allow_empty: bool = False,
) -> tuple[str, bool]:
    """Lexically normalize project-relative paths.

    Replaces backslashes, folds '.' and safe '..', and strictly rejects absolute
    paths, drive letters, UNC paths, and directory traversal escaping the root.
    """
    if raw_value is None or raw_value == "":
        if allow_empty:
            return ".", False
        raise ToolArgsRepairError("path cannot be empty", repair_kind="invalid_args")

    if not isinstance(raw_value, str):
        raise ToolArgsRepairError("path must be a string", repair_kind="invalid_args")

    raw_str = raw_value.strip()
    if not raw_str:
        if allow_empty:
            return ".", False
        raise ToolArgsRepairError("path cannot be empty", repair_kind="invalid_args")

    # Reject Windows drive paths (e.g. C:\foo or C:/foo)
    if _DRIVE_PATH_RE.match(raw_str):
        raise ToolArgsRepairError(
            "paths must be relative to the project root: absolute drive paths are not allowed",
            repair_kind="invalid_args",
        )

    # Reject UNC paths and Unix absolute paths
    normalized_slashes = raw_str.replace("\\", "/")
    if normalized_slashes.startswith("//"):
        raise ToolArgsRepairError(
            "paths must be relative to the project root: UNC paths are not allowed",
            repair_kind="invalid_args",
        )
    if normalized_slashes.startswith("/"):
        raise ToolArgsRepairError(
            "paths must be relative to the project root: absolute paths are not allowed",
            repair_kind="invalid_args",
        )

    segments = normalized_slashes.split("/")
    stack: list[str] = []
    for segment in segments:
        if segment in ("", "."):
            continue
        if segment == "..":
            if not stack:
                raise ToolArgsRepairError(
                    "paths must be relative to the project root: parent directory traversal outside project root is not allowed",
                    repair_kind="invalid_args",
                )
            stack.pop()
        else:
            stack.append(segment)

    result = "/".join(stack) or "."
    repaired = (result != raw_str)
    return result, repaired


def _resolve_path_arg(
    args: Mapping[str, Any],
    *,
    allow_empty: bool = False,
    default_path: str | None = None,
    missing_msg: str | None = None,
) -> tuple[str, int, dict[str, int]]:
    """Resolve and normalize path from args, checking for path/cwd conflicts."""
    present = [k for k in ("path", "cwd") if k in args]
    if len(present) > 1:
        raise ToolArgsRepairError(
            f"conflicting path fields: {', '.join(present)}",
            repair_kind="invalid_args",
        )

    counts: dict[str, int] = {}
    rewrites = 0

    if not present:
        if default_path is not None:
            raw_path = default_path
        elif allow_empty:
            raw_path = "."
        else:
            msg = missing_msg or "path cannot be empty"
            raise ToolArgsRepairError(msg, repair_kind="invalid_args")
    else:
        key = present[0]
        raw_path = args[key]
        if key == "cwd":
            _record_repair(counts, "path_alias")
            rewrites += 1

    norm_path, path_changed = _normalize_path(raw_path, allow_empty=allow_empty)
    if path_changed:
        _record_repair(counts, "path_normalized")
        rewrites += 1

    return norm_path, rewrites, counts


def _require_text_arg(
    args: Mapping[str, Any],
    keys: tuple[str, ...],
    semantic_name: str,
    *,
    missing_msg: str | None = None,
    allow_empty: bool = False,
) -> tuple[str, str | None]:
    """Extract a single string argument from mutually exclusive alias keys.

    Returns (value, alias_key_used_if_not_primary).
    Fails closed if:
    - Multiple keys from the semantic group are present (conflict)
    - Value is not a string
    - Value is blank/empty (unless allow_empty=True)
    """
    present = [k for k in keys if k in args]
    if len(present) > 1:
        raise ToolArgsRepairError(
            f"conflicting {semantic_name} fields: {', '.join(present)}",
            repair_kind="invalid_args",
        )
    if not present:
        if allow_empty:
            return "", None
        msg = missing_msg or f"{semantic_name} requires a value"
        raise ToolArgsRepairError(msg, repair_kind="invalid_args")

    key = present[0]
    val = args[key]
    if not isinstance(val, str):
        raise ToolArgsRepairError(
            f"{semantic_name} must be a string",
            repair_kind="invalid_args",
        )

    if not allow_empty and not val.strip():
        raise ToolArgsRepairError(
            f"{semantic_name} cannot be empty or whitespace",
            repair_kind="invalid_args",
        )

    alias_used = key if key != keys[0] else None
    return val, alias_used


def _bounded_positive_int(
    value: object,
    name: str,
    default: int,
    maximum: int | None = None,
) -> tuple[int, bool]:
    """Parse and bound positive integers, accepting numeric strings."""
    if value is None:
        return default, False

    if isinstance(value, bool):
        raise ToolArgsRepairError(
            f"{name} must be a positive integer",
            repair_kind="invalid_args",
        )

    coerced = False
    if isinstance(value, int):
        parsed = value
    elif isinstance(value, str) and value.strip().isdigit():
        parsed = int(value.strip())
        coerced = True
    else:
        raise ToolArgsRepairError(
            f"{name} must be a positive integer",
            repair_kind="invalid_args",
        )

    if parsed < 1:
        raise ToolArgsRepairError(
            f"{name} must be a positive integer",
            repair_kind="invalid_args",
        )
    if maximum is not None and parsed > maximum:
        raise ToolArgsRepairError(
            f"{name} must be at most {maximum}",
            repair_kind="invalid_args",
        )

    return parsed, coerced


def _normalize_edit(
    args: Mapping[str, Any],
    limits: ToolArgLimits,
) -> ToolArgsRepairResult:
    norm_path, rewrites, counts = _resolve_path_arg(
        args,
        missing_msg="edit requires a top-level path and can only edit one file",
    )
    call_args: dict[str, Any] = {"path": norm_path}

    has_content = "content" in args
    has_replacements = "replacements" in args

    single_old_keys = ("old_string", "search", "old", "before")
    single_new_keys = ("new_string", "replace", "replacement", "after", "new")
    has_single = any(k in args for k in (*single_old_keys, *single_new_keys))

    if sum((has_content, has_replacements, has_single)) != 1:
        raise ToolArgsRepairError(
            "edit requires exactly one mode: content, old_string/new_string, or replacements",
            repair_kind="invalid_args",
        )

    if has_content:
        raw_content = args["content"]
        if not isinstance(raw_content, str):
            raise ToolArgsRepairError(
                "edit content must be a string",
                repair_kind="invalid_args",
            )
        call_args["content"] = raw_content
        return ToolArgsRepairResult(
            args=call_args,
            alias_rewrite_count=rewrites,
            arg_repair_counts=counts,
        )

    if has_replacements:
        raw_replacements = args.get("replacements")

        if isinstance(raw_replacements, str):
            try:
                parsed_replacements = json.loads(raw_replacements)
            except (json.JSONDecodeError, ValueError) as exc:
                raise ToolArgsRepairError(
                    "edit replacements JSON string could not be parsed",
                    repair_kind="invalid_args",
                ) from exc
            _record_repair(counts, "json_replacements_parsed")
            rewrites += 1
            raw_replacements = parsed_replacements

        if isinstance(raw_replacements, dict):
            raw_replacements = [raw_replacements]
            _record_repair(counts, "replacement_object_wrapped")
            rewrites += 1

        if not isinstance(raw_replacements, list) or not raw_replacements:
            raise ToolArgsRepairError(
                "edit replacements must be a non-empty list",
                repair_kind="invalid_args",
            )

        if len(raw_replacements) > limits.max_replacements:
            raise ToolArgsRepairError(
                f"edit supports at most {limits.max_replacements} replacements",
                repair_kind="invalid_args",
            )

        normalized_replacements: list[dict[str, str]] = []
        for item in raw_replacements:
            if not isinstance(item, dict):
                raise ToolArgsRepairError(
                    "every edit replacement must be an object",
                    repair_kind="invalid_args",
                )
            if "path" in item or "cwd" in item:
                raise ToolArgsRepairError(
                    "edit replacements apply to the top-level path only; "
                    "use separate edit calls for different files",
                    repair_kind="invalid_args",
                )

            old_val, old_alias = _require_text_arg(
                item,
                single_old_keys,
                "old_string",
                allow_empty=False,
            )
            if old_alias:
                _record_repair(counts, "edit_field_alias")
                rewrites += 1

            new_val, new_alias = _require_text_arg(
                item,
                single_new_keys,
                "new_string",
                allow_empty=True,
            )
            if new_alias:
                _record_repair(counts, "edit_field_alias")
                rewrites += 1

            normalized_replacements.append({
                "search": old_val,
                "replace": new_val,
            })

        call_args["replacements"] = normalized_replacements
        return ToolArgsRepairResult(
            args=call_args,
            alias_rewrite_count=rewrites,
            arg_repair_counts=counts,
        )

    # Single replacement mode
    old_val, old_alias = _require_text_arg(
        args,
        single_old_keys,
        "old_string",
        allow_empty=False,
    )
    if old_alias:
        _record_repair(counts, "edit_field_alias")
        rewrites += 1

    new_val, new_alias = _require_text_arg(
        args,
        single_new_keys,
        "new_string",
        allow_empty=True,
    )
    if new_alias:
        _record_repair(counts, "edit_field_alias")
        rewrites += 1

    call_args["replacements"] = [{"search": old_val, "replace": new_val}]
    return ToolArgsRepairResult(
        args=call_args,
        alias_rewrite_count=rewrites,
        arg_repair_counts=counts,
    )


def _normalize_read(
    args: Mapping[str, Any],
    limits: ToolArgLimits,
) -> ToolArgsRepairResult:
    norm_path, rewrites, counts = _resolve_path_arg(
        args,
        missing_msg="read requires a path",
    )
    call_args: dict[str, Any] = {"path": norm_path}

    if "offset" in args:
        offset, coerced = _bounded_positive_int(args.get("offset"), "offset", 1)
        if coerced:
            _record_repair(counts, "numeric_coerced")
            rewrites += 1
        call_args["offset"] = offset

    if "limit" in args:
        limit, coerced = _bounded_positive_int(
            args.get("limit"),
            "limit",
            limits.read_default_lines,
            limits.read_max_lines,
        )
        if coerced:
            _record_repair(counts, "numeric_coerced")
            rewrites += 1
        call_args["limit"] = limit

    return ToolArgsRepairResult(
        args=call_args,
        alias_rewrite_count=rewrites,
        arg_repair_counts=counts,
    )


def _normalize_ls(args: Mapping[str, Any]) -> ToolArgsRepairResult:
    norm_path, rewrites, counts = _resolve_path_arg(args, allow_empty=True, default_path=".")
    return ToolArgsRepairResult(
        args={"path": norm_path},
        alias_rewrite_count=rewrites,
        arg_repair_counts=counts,
    )


def _normalize_search(args: Mapping[str, Any]) -> ToolArgsRepairResult:
    query_val, query_alias = _require_text_arg(
        args,
        ("query", "pattern"),
        "query",
        missing_msg="grep requires a query",
        allow_empty=False,
    )
    counts: dict[str, int] = {}
    rewrites = 0
    if query_alias:
        _record_repair(counts, "search_field_alias")
        rewrites += 1

    norm_path, path_rewrites, path_counts = _resolve_path_arg(args, allow_empty=True, default_path=".")
    rewrites += path_rewrites
    for k, v in path_counts.items():
        counts[k] = counts.get(k, 0) + v

    return ToolArgsRepairResult(
        args={"query": query_val, "path": norm_path},
        alias_rewrite_count=rewrites,
        arg_repair_counts=counts,
    )


def _normalize_references(args: Mapping[str, Any]) -> ToolArgsRepairResult:
    symbol_val, symbol_alias = _require_text_arg(
        args,
        ("symbol", "name"),
        "symbol",
        missing_msg="find_references requires a symbol",
        allow_empty=False,
    )
    counts: dict[str, int] = {}
    rewrites = 0
    if symbol_alias:
        _record_repair(counts, "references_field_alias")
        rewrites += 1

    norm_path, path_rewrites, path_counts = _resolve_path_arg(args, allow_empty=True, default_path=".")
    rewrites += path_rewrites
    for k, v in path_counts.items():
        counts[k] = counts.get(k, 0) + v

    return ToolArgsRepairResult(
        args={"symbol": symbol_val.strip(), "path": norm_path},
        alias_rewrite_count=rewrites,
        arg_repair_counts=counts,
    )


def _normalize_run_shell(
    tool_name: str,
    args: Mapping[str, Any],
) -> ToolArgsRepairResult:
    cmd_val, cmd_alias = _require_text_arg(
        args,
        ("command", "cmd"),
        "command",
        missing_msg=f"{tool_name} requires a command",
        allow_empty=False,
    )
    counts: dict[str, int] = {}
    rewrites = 0
    if cmd_alias:
        _record_repair(counts, "command_field_alias")
        rewrites += 1

    norm_path, path_rewrites, path_counts = _resolve_path_arg(args, allow_empty=True, default_path=".")
    rewrites += path_rewrites
    for k, v in path_counts.items():
        counts[k] = counts.get(k, 0) + v

    return ToolArgsRepairResult(
        args={"command": cmd_val, "path": norm_path},
        alias_rewrite_count=rewrites,
        arg_repair_counts=counts,
    )


def normalize_tool_args(
    tool_name: str,
    args: Mapping[str, Any],
    *,
    limits: ToolArgLimits | None = None,
) -> ToolArgsRepairResult:
    """Normalize and repair raw arguments for canonical runtime tools.

    tool_name must be a canonical runtime tool name (e.g. edit, read, search,
    references, ls, run, shell).
    """
    effective_limits = limits or ToolArgLimits()
    normalized_tool = tool_name.lower().strip()

    if normalized_tool == "edit":
        return _normalize_edit(args, effective_limits)
    if normalized_tool == "read":
        return _normalize_read(args, effective_limits)
    if normalized_tool == "ls":
        return _normalize_ls(args)
    if normalized_tool == "search":
        return _normalize_search(args)
    if normalized_tool == "references":
        return _normalize_references(args)
    if normalized_tool in ("run", "shell"):
        return _normalize_run_shell(normalized_tool, args)

    raise ToolArgsRepairError(
        f"unsupported runtime tool: {normalized_tool}",
        repair_kind="unknown_tool",
    )
