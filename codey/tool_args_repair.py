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

PATH_ARG_KEYS = ("path", "cwd")
SEARCH_QUERY_KEYS = ("query", "pattern")
REFERENCES_SYMBOL_KEYS = ("symbol", "name")
COMMAND_KEYS = ("command", "cmd")
EDIT_OLD_KEYS = ("old_string", "search", "old", "before")
EDIT_NEW_KEYS = ("new_string", "replace", "replacement", "after", "new")

ARG_REPAIR_POLICY = {
    "path_alias": "cwd -> path",
    "path_normalized": "lexical path normalization",
    "search_field_alias": "pattern -> query",
    "references_field_alias": "name -> symbol",
    "command_field_alias": "cmd -> command",
    "edit_field_alias": "old/search/before -> old_string, replace/replacement/after/new -> new_string",
    "numeric_coerced": "numeric string -> int",
    "json_replacements_parsed": "JSON string -> replacements list",
    "replacement_object_wrapped": "single object -> list",
}


@dataclass(frozen=True)
class ToolArgLimits:
    max_replacements: int = 8
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


def _reject_unknown_args(
    args: Mapping[str, Any],
    allowed_keys: set[str],
    *,
    context: str,
) -> None:
    if any(not isinstance(key, str) or key not in allowed_keys for key in args):
        raise ToolArgsRepairError(
            f"{context} contains unsupported fields",
            repair_kind="invalid_args",
        )


def _normalize_path(
    raw_value: object,
) -> tuple[str, bool]:
    """Lexically normalize project-relative paths.

    Replaces backslashes, folds '.' and safe '..', and strictly rejects absolute
    paths, drive letters, UNC paths, and directory traversal escaping the root.
    """
    if not isinstance(raw_value, str):
        raise ToolArgsRepairError("path must be a string", repair_kind="invalid_args")

    raw_str = raw_value.strip()
    if not raw_str:
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
    repaired = result != raw_value
    return result, repaired


def _resolve_path_arg(
    args: Mapping[str, Any],
    *,
    default_path: str | None = None,
    missing_msg: str | None = None,
) -> tuple[str, int, dict[str, int]]:
    """Resolve and normalize path from args, checking for path/cwd conflicts."""
    present = [k for k in PATH_ARG_KEYS if k in args]
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
        else:
            msg = missing_msg or "path cannot be empty"
            raise ToolArgsRepairError(msg, repair_kind="invalid_args")
    else:
        key = present[0]
        raw_path = args[key]
        if key == "cwd":
            _record_repair(counts, "path_alias")
            rewrites += 1

    norm_path, path_changed = _normalize_path(raw_path)
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
    allow_blank: bool = False,
    allow_missing: bool = False,
) -> tuple[str, str | None]:
    """Extract a single string argument from mutually exclusive alias keys.

    Returns (value, alias_key_used_if_not_primary).
    Fails closed if:
    - Multiple keys from the semantic group are present (conflict)
    - Value is not a string
    - Value is missing (unless allow_missing=True)
    - Value is blank/empty (unless allow_blank=True)
    """
    present = [k for k in keys if k in args]
    if len(present) > 1:
        raise ToolArgsRepairError(
            f"conflicting {semantic_name} fields: {', '.join(present)}",
            repair_kind="invalid_args",
        )
    if not present:
        if allow_missing:
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

    if not allow_blank and not val.strip():
        raise ToolArgsRepairError(
            f"{semantic_name} cannot be empty or whitespace",
            repair_kind="invalid_args",
        )

    alias_used = key if key != keys[0] else None
    return val, alias_used


def _bounded_positive_int(
    value: object,
    name: str,
    maximum: int | None = None,
) -> tuple[int, bool]:
    """Parse and bound positive integers, accepting numeric strings."""
    if value is None:
        raise ToolArgsRepairError(
            f"{name} must be a positive integer",
            repair_kind="invalid_args",
        )

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
    single_old_keys = EDIT_OLD_KEYS
    single_new_keys = EDIT_NEW_KEYS
    _reject_unknown_args(
        args,
        {"path", "cwd", "content", "replacements", *single_old_keys, *single_new_keys},
        context="edit",
    )
    norm_path, rewrites, counts = _resolve_path_arg(
        args,
        missing_msg="edit requires a top-level path and can only edit one file",
    )
    call_args: dict[str, Any] = {"path": norm_path}

    has_content = "content" in args
    has_replacements = "replacements" in args

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
            _reject_unknown_args(
                item,
                {*single_old_keys, *single_new_keys},
                context="edit replacement",
            )

            old_val, old_alias = _require_text_arg(
                item,
                single_old_keys,
                "old_string",
                allow_blank=False,
            )
            if old_alias:
                _record_repair(counts, "edit_field_alias")
                rewrites += 1

            new_val, new_alias = _require_text_arg(
                item,
                single_new_keys,
                "new_string",
                allow_blank=True,
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
        allow_blank=False,
    )
    if old_alias:
        _record_repair(counts, "edit_field_alias")
        rewrites += 1

    new_val, new_alias = _require_text_arg(
        args,
        single_new_keys,
        "new_string",
        allow_blank=True,
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
    _reject_unknown_args(args, {"path", "cwd", "offset", "limit"}, context="read")
    norm_path, rewrites, counts = _resolve_path_arg(
        args,
        missing_msg="read requires a path",
    )
    call_args: dict[str, Any] = {"path": norm_path}

    if "offset" in args:
        offset, coerced = _bounded_positive_int(args.get("offset"), "offset")
        if coerced:
            _record_repair(counts, "numeric_coerced")
            rewrites += 1
        call_args["offset"] = offset

    if "limit" in args:
        limit, coerced = _bounded_positive_int(
            args.get("limit"),
            "limit",
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
    _reject_unknown_args(args, {"path", "cwd"}, context="list_dir")
    norm_path, rewrites, counts = _resolve_path_arg(args, default_path=".")
    return ToolArgsRepairResult(
        args={"path": norm_path},
        alias_rewrite_count=rewrites,
        arg_repair_counts=counts,
    )


def _normalize_search(args: Mapping[str, Any]) -> ToolArgsRepairResult:
    _reject_unknown_args(args, {*SEARCH_QUERY_KEYS, *PATH_ARG_KEYS}, context="grep")
    query_val, query_alias = _require_text_arg(
        args,
        SEARCH_QUERY_KEYS,
        "query",
        missing_msg="grep requires a query",
        allow_blank=False,
    )
    counts: dict[str, int] = {}
    rewrites = 0
    if query_alias:
        _record_repair(counts, "search_field_alias")
        rewrites += 1

    norm_path, path_rewrites, path_counts = _resolve_path_arg(args, default_path=".")
    rewrites += path_rewrites
    for k, v in path_counts.items():
        counts[k] = counts.get(k, 0) + v

    return ToolArgsRepairResult(
        args={"query": query_val, "path": norm_path},
        alias_rewrite_count=rewrites,
        arg_repair_counts=counts,
    )


def _normalize_references(args: Mapping[str, Any]) -> ToolArgsRepairResult:
    _reject_unknown_args(args, {*REFERENCES_SYMBOL_KEYS, *PATH_ARG_KEYS}, context="find_references")
    symbol_val, symbol_alias = _require_text_arg(
        args,
        REFERENCES_SYMBOL_KEYS,
        "symbol",
        missing_msg="find_references requires a symbol",
        allow_blank=False,
    )
    counts: dict[str, int] = {}
    rewrites = 0
    if symbol_alias:
        _record_repair(counts, "references_field_alias")
        rewrites += 1

    norm_path, path_rewrites, path_counts = _resolve_path_arg(args, default_path=".")
    rewrites += path_rewrites
    for k, v in path_counts.items():
        counts[k] = counts.get(k, 0) + v

    return ToolArgsRepairResult(
        args={"symbol": symbol_val, "path": norm_path},
        alias_rewrite_count=rewrites,
        arg_repair_counts=counts,
    )


def _normalize_run_shell(
    tool_name: str,
    args: Mapping[str, Any],
) -> ToolArgsRepairResult:
    _reject_unknown_args(args, {*COMMAND_KEYS, *PATH_ARG_KEYS}, context=tool_name)
    cmd_val, cmd_alias = _require_text_arg(
        args,
        COMMAND_KEYS,
        "command",
        missing_msg=f"{tool_name} requires a command",
        allow_blank=False,
    )
    counts: dict[str, int] = {}
    rewrites = 0
    if cmd_alias:
        _record_repair(counts, "command_field_alias")
        rewrites += 1

    norm_path, path_rewrites, path_counts = _resolve_path_arg(args, default_path=".")
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
