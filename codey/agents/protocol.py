"""Agent JSON protocol repair, edit parsing, and verification text helpers."""

from __future__ import annotations

import json
import re
from pathlib import Path

from codey.completion.verification_policy import VerificationCandidate
from codey.protocols import ProtocolCodec
from codey.protocols.json_codec import (
    PROTOCOL_DIRECT_ANSWER,
    PROTOCOL_DISALLOWED_TOOL,
    PROTOCOL_INVALID_ARGS,
    PROTOCOL_NATIVE_TOOL_DENIAL,
    PROTOCOL_NESTED_TOOL_IN_DONE,
    PROTOCOL_NO_JSON,
    PROTOCOL_UNKNOWN_TOOL,
    _balanced_json_objects,
)
from codey.runtime.models import ToolCall, ToolPlan
from codey.toolchain.runtime import EditBlock, safe_join

VERIFICATION_REQUEST_RE = re.compile(
    r"\b("
    r"run|test|tests|unittest|pytest|verify|verification|check|build|lint|typecheck"
    r")\b|跑测试|运行测试|测试通过|验证|检查",
    re.IGNORECASE,
)
VERIFICATION_NEGATION_PREFIX_RE = re.compile(
    r"(?:"
    r"\b(?:do\s+not|don't|dont|never)\s+(?:(?:run|execute|perform|do)\s+)?(?:any\s+|the\s+)?"
    r"|\b(?:without|skip|avoid)\s+(?:running\s+|run\s+|testing\s+|checking\s+)?"
    r"|\bno\s+need\s+(?:to\s+|for\s+)?"
    r"|\bneed(?:n't| not)\s+(?:to\s+)?"
    r"|(?:不要|不用|无需|不需要|别|不必)(?:运行|跑|执行|做|进行)?(?:任何)?"
    r")$",
    re.IGNORECASE,
)
VERIFICATION_NEGATION_CANCEL_RE = re.compile(
    r"\b(?:do\s+not|don't|dont|never)\s+(?:skip|avoid)\s+(?:[\w'-]+\s+){0,5}$",
    re.IGNORECASE,
)
VERIFICATION_FORBID_RE = re.compile(
    r"\b(?:do\s+not|don't|dont|never)\s+"
    r"(?:(?:run|execute|perform|do)\s+)?(?:any\s+|the\s+)?"
    r"(?:commands?|checks?|tests?|testing|unittest|pytest|verification|verify|build|lint|typecheck)\b"
    r"|\b(?:without|skip|avoid)\s+"
    r"(?:running\s+|run\s+|testing\s+|checking\s+)?"
    r"(?:commands?|checks?|tests?|testing|unittest|pytest|verification|verify|build|lint|typecheck)\b"
    r"|\bno\s+need\s+(?:to\s+|for\s+)?"
    r"(?:run|test|verify|check|build|lint|typecheck|commands?|checks?|tests?)\b"
    r"|\bneed(?:n't| not)\s+(?:to\s+)?"
    r"(?:run|test|verify|check|build|lint|typecheck)\b"
    r"|(?:不要|不用|无需|不需要|别|不必)(?:运行|跑|执行|做|进行)?(?:任何)?(?:命令|测试|检查|验证)",
    re.IGNORECASE,
)


def edit_blocks_from_call(call: ToolCall) -> list[EditBlock]:
    replacements = call.args.get("replacements")
    if not isinstance(replacements, list):
        return []
    blocks: list[EditBlock] = []
    for item in replacements:
        if not isinstance(item, dict):
            continue
        search = str(item.get("search") or "")
        replace = str(item.get("replace") or "")
        blocks.append(EditBlock(search, replace))
    return blocks


def edit_has_content(call: ToolCall) -> bool:
    return "content" in call.args


def canonical_project_path(root: Path, rel: str) -> str:
    path = safe_join(root, rel)
    return path.relative_to(root.resolve()).as_posix()


def verification_match_is_negated(task: str, start: int) -> bool:
    prefix = re.split(r"[\n.;!?。！？；]", task[:start])[-1]
    if VERIFICATION_NEGATION_CANCEL_RE.search(prefix):
        return False
    return bool(VERIFICATION_NEGATION_PREFIX_RE.search(prefix))


def task_requests_verification(task: str) -> bool:
    text = str(task or "")
    return any(
        not verification_match_is_negated(text, match.start())
        for match in VERIFICATION_REQUEST_RE.finditer(text)
    )


def task_forbids_verification(task: str) -> bool:
    text = str(task or "")
    return bool(VERIFICATION_FORBID_RE.search(text)) and not task_requests_verification(text)


def verification_reminder(task: str) -> str:
    return (
        "The user asked for verification, and files were changed, but no run "
        "tool call after the latest edit has been observed yet. Reply with "
        "exactly one JSON object that calls run now, such as "
        '{"tool":"run","args":{"command":"python -m unittest","path":"."}}. '
        "After the run result is green, call done. Original task:\n"
        f"{task}"
    )


def default_verification_reminder(candidate: VerificationCandidate) -> str:
    call = json.dumps(
        {
            "tool": "run",
            "args": {"command": candidate.command, "path": candidate.cwd},
        },
        separators=(",", ":"),
    )
    return (
        "Files changed and a trusted local check is available. Run this check "
        "after the latest edit before completing:\n\n"
        f"{call}"
    )


def protocol_repair_prompt(
    codec: ProtocolCodec,
    plan: ToolPlan,
    *,
    previous_reply: str = "",
) -> str:
    error = str(plan.protocol_error or "invalid JSON tool call")
    kind = str(plan.protocol_error_kind or "").strip()
    previous = previous_tool_object(previous_reply, codec, kind, error)
    lines = [
        f"Protocol error: {repair_error_summary(error, kind)}",
        "",
        "Preserve the previous intended path, content, old_string/new_string, "
        "command, and other valid arguments when possible; only fix the schema "
        "or tool-contract error.",
        "Examples below show schema only; do not replace valid previous values "
        "with placeholder or example values.",
        "",
    ]

    if kind == PROTOCOL_UNKNOWN_TOOL:
        tool = unknown_tool_from_error(error)
        edit_example = codec_public_example(codec, "edit")
        if tool in {"write", "write_file", "create_file"} and edit_example:
            example = unknown_write_repair_example(previous)
            lines.extend((
                "The previous reply used an unknown write tool. Coding has no "
                f"{tool} tool.",
                "Create a new file with edit(content=...), or modify an existing "
                "file with edit(old_string/new_string).",
            ))
            if example:
                lines.extend(("", "Example preserving your previous intent:", example))
        else:
            example = preferred_read_example(codec)
            lines.extend((
                "The previous reply used an unknown local tool.",
                "Use only the coding JSON tools listed in the system prompt.",
            ))
            if example:
                lines.extend(("", "Example:", example))
    elif kind == PROTOCOL_DISALLOWED_TOOL:
        example = preferred_read_example(codec) or codec_public_example(codec, "done")
        lines.extend((
            "The previous reply used a tool that exists, but this current phase "
            "does not allow it.",
            "Use only the tools listed in the system prompt for this phase.",
        ))
        if example:
            lines.extend(("", "Example:", example))
    elif kind == PROTOCOL_INVALID_ARGS:
        lines.extend(invalid_args_repair_lines(error, previous, codec))
    elif kind == PROTOCOL_DIRECT_ANSWER:
        lines.extend((
            "The previous reply answered in prose.",
            "Coding replies must be one local-runner JSON command.",
            "If the task is complete, put the final user-facing answer in done.summary.",
            "",
            "Example:",
            '{"tool":"done","args":{"summary":"finished"}}',
        ))
    elif kind == PROTOCOL_NATIVE_TOOL_DENIAL:
        lines.extend((
            "Ignore website-native tool availability messages.",
            "These are local-runner JSON commands; the local runner executes them after you reply.",
            "",
            "Example:",
            preferred_read_example(codec),
        ))
    elif kind == PROTOCOL_NESTED_TOOL_IN_DONE:
        example = nested_tool_repair_example(previous, codec)
        lines.extend((
            "done.summary cannot contain another JSON tool call.",
            "If you intended to run a tool, call that tool directly instead of wrapping it in done.",
        ))
        if example:
            lines.extend(("", "Example preserving your previous intent:", example))
        else:
            lines.extend((
                "",
                "Example:",
                codec_public_example(codec, "run") or preferred_read_example(codec),
            ))
    elif kind == PROTOCOL_NO_JSON:
        example = preferred_read_example(codec) or codec_public_example(codec, "done")
        lines.extend((
            "Reply with a JSON tool call, not prose.",
        ))
        if example:
            lines.extend(("", "Example:", example))
    else:
        lines.append(codec.repair_prompt())

    lines.extend((
        "",
        "Reply with exactly one JSON object, no markdown fences and no other text.",
    ))
    return "\n".join(lines)


def unknown_tool_from_error(error: str) -> str:
    match = re.search(r"unknown tool:\s*([A-Za-z0-9_-]+)", error)
    return match.group(1).strip().lower() if match else ""


def repair_error_summary(error: str, kind: str) -> str:
    if kind == PROTOCOL_UNKNOWN_TOOL:
        tool = unknown_tool_from_error(error)
        if tool:
            return f"unknown tool: {tool}"
    return error


def invalid_args_repair_lines(
    error: str,
    previous: dict[str, object] | None = None,
    codec: ProtocolCodec | None = None,
) -> list[str]:
    folded = error.lower()
    if "positive integer" in folded and "offset" in folded:
        lines = [
            "read_file offset is 1-based and must be a positive integer.",
            "Use offset=1 or omit offset when reading from the beginning.",
            "Keep the same path and any valid limit from the previous read_file call.",
        ]
        example = read_offset_repair_example(previous)
        if example:
            lines.extend(("", "Example preserving your previous intent:", example))
        return lines
    if "exactly one mode" in folded:
        lines = [
            "edit requires exactly one mode: content, old_string/new_string, or replacements.",
            "Use content only for a new file. For an existing file, use exact old_string/new_string.",
            "If old_string/new_string were already present, copy those strings exactly, including escaped \\n.",
        ]
        example = edit_mode_repair_example(previous)
        if example:
            lines.extend(("", "Example preserving your previous intent:", example))
        return lines
    if "top-level path" in folded:
        return [
            "edit requires one top-level path and can only edit one file per call.",
            "",
            "Example:",
            '{"tool":"edit","args":{"path":"app.py","old_string":"old exact text","new_string":"new text"}}',
        ]
    if "read_files" in folded:
        return [
            "read_files requires a non-empty paths list.",
            "",
            "Example:",
            codec_public_example(codec, "read_files"),
        ]
    if "parallel" in folded:
        return [
            "parallel accepts only read-only list_dir, read_file, and grep calls.",
            "",
            "Example:",
            codec_public_example(codec, "parallel"),
        ]
    if "grep requires a query" in folded:
        return [
            "grep requires a non-empty query.",
            "",
            "Example:",
            codec_public_example(codec, "grep"),
        ]
    if "find_references requires a symbol" in folded:
        return [
            "find_references requires a symbol.",
            "",
            "Example:",
            codec_public_example(codec, "find_references"),
        ]
    if "requires a command" in folded:
        example = codec_public_example(codec, "run") or codec_public_example(codec, "shell")
        return [
            "run and shell require a command string.",
            "",
            "Example:",
            example,
        ]
    return [
        "The tool name was recognized, but its arguments do not match the coding schema.",
        "Use one valid JSON shape from the tool contract.",
        "",
        "Example:",
        preferred_read_example(codec),
    ]


def codec_public_example(codec: ProtocolCodec | None, tool_name: str) -> str:
    getter = getattr(codec, "public_example", None)
    if callable(getter):
        try:
            return str(getter(tool_name) or "")
        except (TypeError, ValueError):
            return ""
    return ""


def preferred_read_example(codec: ProtocolCodec | None) -> str:
    for tool_name in ("read_file", "list_dir", "grep"):
        example = codec_public_example(codec, tool_name)
        if example:
            return example
    return ""


def previous_tool_object(
    previous_reply: str,
    codec: ProtocolCodec,
    kind: str,
    error: str,
) -> dict[str, object] | None:
    objects = _balanced_json_objects(previous_reply)
    if kind == PROTOCOL_UNKNOWN_TOOL:
        target_tool = unknown_tool_from_error(error)
        if target_tool:
            for obj in objects:
                tool = str(obj.get("tool") or obj.get("name") or "").strip().lower()
                if tool == target_tool:
                    return dict(obj)
    if kind in {
        PROTOCOL_INVALID_ARGS,
        PROTOCOL_NESTED_TOOL_IN_DONE,
        PROTOCOL_DISALLOWED_TOOL,
    }:
        for obj in objects:
            tool = str(obj.get("tool") or obj.get("name") or "").strip()
            if not tool:
                continue
            plan = codec.parse(json_example(dict(obj)))
            if plan.protocol_error_kind == kind:
                return dict(obj)
    for obj in objects:
        tool = str(obj.get("tool") or obj.get("name") or "").strip()
        if tool:
            return dict(obj)
    return None


def previous_args(previous: dict[str, object] | None) -> dict[str, object]:
    if not previous:
        return {}
    args = previous.get("args")
    if isinstance(args, dict):
        return dict(args)
    return {
        str(key): value
        for key, value in previous.items()
        if key not in {"tool", "name"}
    }


def json_example(value: dict[str, object]) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def unknown_write_repair_example(previous: dict[str, object] | None) -> str:
    args = previous_args(previous)
    path = str(args.get("path") or "").strip()
    if not path:
        return '{"tool":"edit","args":{"path":"new_app.py","content":"..."}}'
    fixed_args: dict[str, object] = {"path": path}
    if "content" in args:
        fixed_args["content"] = str(args.get("content") or "")
    elif (single := single_edit_args(args)) is not None:
        fixed_args.update(single)
    else:
        fixed_args["content"] = "..."
    return json_example({"tool": "edit", "args": fixed_args})


def read_offset_repair_example(previous: dict[str, object] | None) -> str:
    args = previous_args(previous)
    path = str(args.get("path") or args.get("cwd") or "").strip()
    if not path:
        return ""
    fixed_args: dict[str, object] = {"path": path, "offset": 1}
    limit = positive_int_value(args.get("limit"))
    if limit is not None:
        fixed_args["limit"] = limit
    return json_example({"tool": "read_file", "args": fixed_args})


def edit_mode_repair_example(previous: dict[str, object] | None) -> str:
    args = previous_args(previous)
    path = str(args.get("path") or "").strip()
    if not path:
        return ""
    fixed_args: dict[str, object] = {"path": path}
    replacements = args.get("replacements")
    if isinstance(replacements, list) and replacements:
        cleaned: list[dict[str, str]] = []
        for item in replacements:
            if not isinstance(item, dict):
                continue
            single = single_edit_args(item)
            if single is not None:
                cleaned.append(single)
        if cleaned:
            fixed_args["replacements"] = cleaned
            return json_example({"tool": "edit", "args": fixed_args})
    if (single := single_edit_args(args)) is not None:
        fixed_args.update(single)
        return json_example({"tool": "edit", "args": fixed_args})
    fixed_args["old_string"] = "old exact text\n"
    fixed_args["new_string"] = "new text\n"
    return json_example({"tool": "edit", "args": fixed_args})


def single_edit_args(args: dict[str, object]) -> dict[str, str] | None:
    old = args.get("old_string", args.get("search"))
    new = args.get("new_string", args.get("replace", args.get("replacement")))
    if old is None or new is None or not str(old):
        return None
    return {"old_string": str(old), "new_string": str(new)}


def positive_int_value(value: object) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int) and value > 0:
        return value
    if isinstance(value, str) and value.isdigit():
        number = int(value)
        if number > 0:
            return number
    return None


def nested_tool_repair_example(
    previous: dict[str, object] | None,
    codec: ProtocolCodec | None = None,
) -> str:
    tool = str((previous or {}).get("tool") or (previous or {}).get("name") or "")
    if tool.lower().strip() != "done":
        return ""
    summary = previous_args(previous).get("summary")
    if not isinstance(summary, str):
        return ""
    try:
        nested = json.loads(summary)
    except json.JSONDecodeError:
        return ""
    if not isinstance(nested, dict):
        return ""
    nested_tool = str(nested.get("tool") or nested.get("name") or "").strip()
    if not nested_tool:
        return ""
    if codec is not None and not codec_public_example(codec, nested_tool):
        return ""
    return json_example(nested)
