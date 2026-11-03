"""Canonical persisted argument shapes for safe tool replay."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

from codey.runtime.effects.replay_policy import IDEMPOTENT_RUNTIME_TOOL_NAMES, is_replayable_safe_tool
from codey.toolchain.definition import TOOL_DEFINITION_BY_NAME
from codey.toolchain.tool_args_repair import ToolArgLimits, normalize_tool_args

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
        allowed=frozenset(key for key, _schema in TOOL_DEFINITION_BY_NAME["grep"].parameters),
        required=frozenset({"path", "query"}),
    ),
    "references": ReplayArgSpec(
        allowed=frozenset({"path", "symbol"}),
        required=frozenset({"path", "symbol"}),
    ),
}
REPLAY_ARG_TOOL_NAMES = frozenset(_REPLAY_ARG_SPECS) | IDEMPOTENT_RUNTIME_TOOL_NAMES | {"codemode"}


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


def _validate_knowledge_commit(args: Mapping[str, object]) -> dict[str, object]:
    import json

    if set(args) != {"commit_id", "changes"} or not isinstance(args["changes"], list):
        raise ValueError("knowledge replay requires its original commit_id and changes")
    _validate_replay_text_arg(args["commit_id"], "commit_id")
    if not 1 <= len(args["changes"]) <= 128:
        raise ValueError("knowledge replay batch is out of bounds")
    for row in args["changes"]:
        if (not isinstance(row, dict) or set(row) != {"value", "expected_revision"}
                or not isinstance(row["value"], dict)
                or type(row["expected_revision"]) is not int or row["expected_revision"] < 0):
            raise ValueError("invalid knowledge replay mutation")
    # Domain validation remains in the knowledge executor. Recovery preserves the
    # exact JSON payload; no alias repair, model call or speculative rewriting.
    json.dumps(dict(args), allow_nan=False)
    return dict(args)


def _validate_research_answer(args: Mapping[str, object]) -> dict[str, object]:
    import json

    state = args.get("state")
    if not isinstance(state, str) or state not in {"answered", "partial", "unresolved"}:
        raise ValueError("research answer replay requires its explicit original state")
    if set(args) in ({"state", "markdown"}, {"state", "markdown", "knowledge_updates"}):
        if not isinstance(args["markdown"], str) or not 12 <= len(args["markdown"].strip()) <= 64000:
            raise ValueError("invalid original whole-answer declaration")
        if "knowledge_updates" in args:
            from codey.research.knowledge_updates import MAX_UPDATES

            updates = args["knowledge_updates"]
            if not isinstance(updates, list) or len(updates) > MAX_UPDATES or any(not isinstance(row, dict) for row in updates):
                raise ValueError("invalid original whole-answer knowledge batch")
    elif set(args) == {"state", "findings"}:
        refs = args["findings"]
        if (not isinstance(refs, list) or len(refs) > 16 or any(not isinstance(ref, str) for ref in refs)
                or len(set(refs)) != len(refs) or bool(refs) != (state != "unresolved")):
            raise ValueError("invalid original research selection")
    elif set(args) == {"state", "finding"}:
        if state == "unresolved" or not isinstance(args["finding"], dict):
            raise ValueError("invalid original inline declaration")
    else:
        raise ValueError("research answer replay requires one original selection form")
    # Only preserve shape and exact JSON here. The original frozen contract,
    # scope, passages and knowledge executor validate meaning before any write.
    json.dumps(dict(args), allow_nan=False)
    return dict(args)


def _validate_revision_preparation(args: Mapping[str, object]) -> dict[str, object]:
    if args:
        raise ValueError("revision preparation takes no arguments")
    return {}


def validate_replay_args_shape(
    tool_name: str,
    args: object,
) -> dict[str, object]:
    """Validate persisted replay args without aliases, repairs, or defaults."""
    canonical_name = (tool_name or "").strip()
    if not is_replayable_safe_tool(canonical_name):
        raise ValueError(f"tool is not replayable: {canonical_name}")
    if not isinstance(args, Mapping):
        raise ValueError("replay args must be a mapping")
    validator = {"knowledge_commit": _validate_knowledge_commit, "submit_research_answer": _validate_research_answer,
                 "prepare_knowledge_revision": _validate_revision_preparation}.get(canonical_name)
    if validator is not None:
        return validator(args)
    if canonical_name == "submit_research_review":
        import json

        if (set(args) != {"basis_digest", "assessment"} or not isinstance(args["basis_digest"], str)
                or len(args["basis_digest"]) != 64 or not isinstance(args["assessment"], dict)):
            raise ValueError("review replay requires its original basis digest and assessment")
        json.dumps(dict(args), allow_nan=False)
        return dict(args)
    if canonical_name == "codemode":
        from codey.codemode.protocol import parse_codemode_args
        from codey.toolchain.tool_spec import spec_for_tool

        codemode_spec = spec_for_tool("codemode")
        if codemode_spec is None or set(args) - {key for key, _schema in codemode_spec.parameters}:
            raise ValueError("unsupported CodeMode replay arguments")
        parse_codemode_args(args)
        return dict(args)

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
        normalized = normalize_tool_args(canonical_name, args)
        if normalized.alias_rewrite_count or normalized.arg_repair_counts or normalized.args != dict(args):
            raise ValueError("search replay arguments must be strictly canonical")
        if "include" in args and len(str(args["include"])) > REPLAY_ARG_TEXT_MAX_CHARS:
            raise ValueError("search include exceeds replay text limit")
        validated.update(normalized.args)
    elif canonical_name == "references":
        validated["symbol"] = _validate_replay_text_arg(args["symbol"], "symbol")
    return validated


__all__ = [
    "REPLAY_ARG_TOOL_NAMES",
    "REPLAY_ARG_TEXT_MAX_CHARS",
    "REPLAY_READ_MAX_LINES",
    "validate_replay_args_shape",
]
