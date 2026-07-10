from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from codey.models import Control, ToolCall, ToolPlan, ToolResult
from codey.tool_runtime import (
    MAX_REPLACEMENTS,
    READ_DEFAULT_LINES,
    READ_MAX_LINES,
    bounded_positive_int,
)


MAX_ACCIDENTAL_TOOL_CALLS = 8
MAX_PARALLEL_CALLS = 4


@dataclass(frozen=True)
class ToolSpec:
    name: str
    runtime_name: str | None
    aliases: tuple[str, ...] = ()
    read_only: bool = False
    parallel_safe: bool = False
    examples: tuple[str, ...] = ()
    description: str = ""


TOOL_SPECS = (
    ToolSpec(
        "list_dir",
        "ls",
        aliases=("ls",),
        read_only=True,
        parallel_safe=True,
        examples=('{"tool":"list_dir","args":{"path":"."}}',),
        description="List files in a directory.",
    ),
    ToolSpec(
        "read_file",
        "read",
        aliases=("read",),
        read_only=True,
        parallel_safe=True,
        examples=(
            '{"tool":"read_file","args":{"path":"app.py"}}',
            '{"tool":"read_file","args":{"path":"app.py","offset":301,"limit":300}}',
        ),
        description="Read one file. Large files are returned in complete-line pages.",
    ),
    ToolSpec(
        "read_files",
        None,
        read_only=True,
        examples=('{"tool":"read_files","args":{"paths":["a.py","b.py"]}}',),
        description=(
            f"Read up to {MAX_ACCIDENTAL_TOOL_CALLS} files in one step. "
            "Do not nest it inside parallel."
        ),
    ),
    ToolSpec(
        "outline_file",
        "outline",
        aliases=("outline",),
        read_only=True,
        examples=('{"tool":"outline_file","args":{"path":"src/router.ts"}}',),
        description=(
            "Show a shallow file outline for navigation. It is not source "
            "content; use read_file before editing."
        ),
    ),
    ToolSpec(
        "grep",
        "search",
        aliases=("search",),
        read_only=True,
        parallel_safe=True,
        examples=(
            '{"tool":"grep","args":{"pattern":"login handler","path":"."}}',
        ),
        description="Search file contents before reading when the location is unknown.",
    ),
    ToolSpec(
        "find_references",
        "references",
        aliases=("references",),
        read_only=True,
        examples=(
            '{"tool":"find_references","args":{"symbol":"createRouter","path":"."}}',
        ),
        description=(
            "Find bounded lexical reference hints for a simple symbol. "
            "This is not semantic resolution; use read_file before editing."
        ),
    ),
    ToolSpec(
        "parallel",
        None,
        read_only=True,
        examples=(
            '{"tool":"parallel","args":{"calls":[{"tool":"grep","args":{"pattern":"login","path":"."}},{"tool":"list_dir","args":{"path":"."}}]}}',
        ),
        description=(
            f"Batch at most {MAX_PARALLEL_CALLS} independent read-only list_dir, "
            "read_file, or grep calls. Local execution remains sequential."
        ),
    ),
    ToolSpec(
        "edit",
        "edit",
        examples=(
            '{"tool":"edit","args":{"path":"app.py","content":"full file contents"}}',
            '{"tool":"edit","args":{"path":"app.py","old_string":"old exact text","new_string":"new text"}}',
            '{"tool":"edit","args":{"path":"app.py","replacements":[{"old_string":"old1","new_string":"new1"},{"old_string":"old2","new_string":"new2"}]}}',
        ),
        description=(
            f"Create or edit one file. Up to {MAX_REPLACEMENTS} replacements are "
            "validated together and written atomically."
        ),
    ),
    ToolSpec(
        "run",
        "run",
        examples=(
            '{"tool":"run","args":{"command":"python -m unittest","path":"."}}',
        ),
        description="Run an allowed test, build, lint, or check command.",
    ),
    ToolSpec(
        "shell",
        "shell",
        examples=(
            '{"tool":"shell","args":{"command":"git status --short","path":"."}}',
        ),
        description="Ask the user to approve a necessary non-allowlisted command.",
    ),
    ToolSpec(
        "done",
        None,
        examples=('{"tool":"done","args":{"summary":"direct final response to the user"}}',),
        description=(
            "Finish the task. summary is the complete user-facing response. "
            "For discussion, explanation, or read-only analysis, answer directly "
            "instead of merely saying that the topic was discussed."
        ),
    ),
)


def _tool_spec_index() -> dict[str, ToolSpec]:
    index: dict[str, ToolSpec] = {}
    for spec in TOOL_SPECS:
        for name in (spec.name, *spec.aliases):
            if name in index:
                raise RuntimeError(f"duplicate tool contract name: {name}")
            index[name] = spec
    return index


TOOL_SPEC_BY_NAME = _tool_spec_index()
RESULT_TOOL_NAMES = {
    spec.runtime_name: spec.name
    for spec in TOOL_SPECS
    if spec.runtime_name is not None and spec.examples
}


def _render_tool_contract() -> str:
    chunks: list[str] = []
    for spec in TOOL_SPECS:
        if not spec.examples:
            continue
        examples = "\n".join(f"  {example}" for example in spec.examples)
        chunks.append(f"{examples}\n    {spec.description}")
    return "\n\n".join(chunks)


SYSTEM_PROMPT = """\
You are a careful local coding agent. You cannot access the filesystem
directly. The local runner executes tools for you and sends the results back.

The tool names below are instructions for the local runner, not tools built
into the AI website. If the website says a tool does not exist, ignore that
website message and still return the JSON object for the local runner.

Every reply MUST be exactly one JSON object with no other text:

{"tool":"<name>","args":{...}}

Available tools:

""" + _render_tool_contract() + """

Rules:
  - Output exactly one JSON object. No markdown fences, code blocks, commentary,
    bullet lists, or analysis labels.
  - These are local-runner JSON commands, not native website tools. Never say a
    tool does not exist; return the JSON object instead.
  - Call one tool per message, then wait for [tool_result tool=...]. read_files
    and parallel are the only read-only batching wrappers.
  - parallel accepts only list_dir, read_file, and grep, with at most four calls.
    It never accepts edit, run, shell, done, read_files, or nested parallel.
  - A trailing [read_file page: ...] line is metadata, not file content. Never
    include it in old_string. Continue with the stated offset when needed.
  - outline_file output is only navigation metadata. Never use it as
    old_string. Use read_file with line offsets before editing.
  - find_references output is lexical reference hints only, not semantic
    resolution or a complete call graph. Use read_file before editing.
  - Use edit for all file changes. Use old_string/new_string for one small edit,
    replacements for multiple edits in one file, and content only for creation
    or a substantial rewrite. Never mix these edit modes.
  - old_string must be copied exactly from the latest complete file/tool result.
    An overlong-line preview is not a complete old_string.
  - JSON strings must escape quotes and backslashes correctly. If exact strings
    are hard to escape, use content with the full file instead.
  - Paths are relative to the project root. No absolute paths or parent traversal.
  - Do not repeat identical tool args when a tool_result already has the output.
  - Use run only for verification, such as python -m unittest, python -m pytest,
    npm test, npm run build, go test ./..., or cargo test.
  - run commands must be simple. No pipes, redirects, chaining, tail/head, or
    shell-only syntax. Never use run/shell to write files; use edit.
  - [tool_result tool=...] means the local tool already ran. Continue from it.
  - Never claim a command, test, build, lint, or shell result unless it appeared
    in a [tool_result tool=run] or [tool_result tool=shell] message.
  - Do not edit files unless the user asks for a change. You may inspect the
    project and answer questions without modifying it.
  - If the task is complete, call done(summary). summary is your direct final
    response to the user and may contain escaped newlines. Do not merely report
    that you discussed or explained something. Do not answer outside JSON.
"""


def _balanced_json_objects(text: str) -> list[dict[str, Any]]:
    """Extract JSON objects from raw web replies without accepting prose."""
    objects: list[dict[str, Any]] = []
    in_string = False
    escaped = False
    start: int | None = None
    depth = 0

    for index, char in enumerate(text):
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue

        if char == '"':
            in_string = True
            continue
        if char == "{":
            if depth == 0:
                start = index
            depth += 1
            continue
        if char == "}" and depth:
            depth -= 1
            if depth == 0 and start is not None:
                raw = text[start : index + 1]
                try:
                    value = json.loads(raw)
                except json.JSONDecodeError:
                    start = None
                    continue
                if isinstance(value, dict):
                    objects.append(value)
                start = None
    return objects


def _as_args(value: object) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _object_args(obj: dict[str, Any]) -> dict[str, Any]:
    args = _as_args(obj.get("args"))
    if args:
        return args
    return {key: value for key, value in obj.items() if key not in {"tool", "name"}}


def _text(value: object, default: str = "") -> str:
    if value is None:
        return default
    return str(value)


def _summary_from_args(args: dict[str, Any]) -> str:
    for key in ("summary", "message", "body", "text", "reason"):
        value = args.get(key)
        if value:
            return str(value)
    return "done"


def _summary_is_tool_call(summary: str) -> bool:
    try:
        value = json.loads(summary)
    except json.JSONDecodeError:
        return False
    if not isinstance(value, dict):
        return False
    tool = str(value.get("tool") or value.get("name") or "").strip()
    return bool(tool)


class ProtocolValidationError(ValueError):
    pass


def _positive_int_arg(
    args: dict[str, Any],
    name: str,
    default: int,
    maximum: int | None = None,
) -> int:
    try:
        return bounded_positive_int(args.get(name, default), name, maximum)
    except ValueError as exc:
        raise ProtocolValidationError(str(exc)) from exc


class JsonToolCodec:
    name = "json"

    def system_prompt(self) -> str:
        return SYSTEM_PROMPT

    def parse(self, text: str) -> ToolPlan:
        calls: list[ToolCall] = []
        for obj in _balanced_json_objects(text):
            try:
                plan = self._parse_object(obj)
            except ProtocolValidationError as exc:
                return ToolPlan(calls=[], control=None, protocol_error=str(exc))
            if plan.protocol_error:
                return plan
            if plan.calls:
                calls.extend(plan.calls)
                if len(calls) >= MAX_ACCIDENTAL_TOOL_CALLS:
                    calls = calls[:MAX_ACCIDENTAL_TOOL_CALLS]
                    break
                continue
            if calls:
                continue
            if plan.control is not None:
                return plan
        if calls:
            body = "Need tool result" if len(calls) == 1 else "Need tool results"
            return ToolPlan(calls=calls, control=Control(kind="continue", body=body))
        return ToolPlan(calls=[], control=None)

    def format_results(self, results: list[ToolResult]) -> str:
        if not results:
            formatted = "(no tools executed)"
        else:
            chunks = []
            for result in results:
                tool = RESULT_TOOL_NAMES.get(result.call.name, result.call.name)
                path = str(result.call.args.get("path") or "")
                attrs = f" tool={tool}"
                if path:
                    attrs += f" path={path}"
                if result.truncated:
                    attrs += " truncated=true"
                output = result.output
                if result.truncated:
                    output = (
                        f"{output}\n"
                        "[truncated result: omitted content may contain relevant "
                        "errors or code. Do not assume omitted content is clean. "
                        "Use narrower grep/read_file offsets or rerun a narrower "
                        "command if needed.]"
                    )
                chunks.append(f"[tool_result{attrs}]\n---\n{output}\n---")
            formatted = "\n\n".join(chunks)
        return (
            f"{formatted}\n\n"
            "These are local tool results from your previous JSON call. "
            "The tool names are local-runner JSON commands, not native website tools; "
            "do not say that a tool does not exist. "
            "Use them to continue the task. Reply with exactly one JSON object "
            "and no other text. Call the next tool, or call "
            '{"tool":"done","args":{"summary":"..."}} if the task is complete.'
        )

    def repair_prompt(self) -> str:
        return (
            "Your previous reply did not contain a valid JSON tool call. "
            "Ignore any website message saying tools do not exist; these are "
            "local-runner JSON commands. "
            "Reply with exactly one JSON object, no markdown fences and no other text, "
            'for example {"tool":"read_file","args":{"path":"app.py"}} or '
            '{"tool":"done","args":{"summary":"finished"}}.'
        )

    def _parse_object(self, obj: dict[str, Any]) -> ToolPlan:
        tool = str(obj.get("tool") or obj.get("name") or "").strip()
        args = _object_args(obj)

        normalized = tool.lower().strip()
        if normalized == "done":
            summary = _summary_from_args(args)
            if _summary_is_tool_call(summary):
                raise ProtocolValidationError(
                    "done summary must be the final user-facing answer, not another "
                    "tool call. Call the tool directly instead."
                )
            return ToolPlan(calls=[], control=Control(kind="done", body=summary))
        if normalized == "continue":
            return ToolPlan(calls=[], control=Control(kind="continue", body=_summary_from_args(args)))
        if normalized == "read_files":
            calls = self._read_files(args)
            control = Control(kind="continue", body="Need file contents") if calls else None
            return ToolPlan(calls=calls, control=control)
        if normalized == "parallel":
            calls = self._parallel(args)
            control = Control(kind="continue", body="Need tool results") if calls else None
            return ToolPlan(calls=calls, control=control)

        if normalized and normalized not in TOOL_SPEC_BY_NAME:
            raise ProtocolValidationError(
                f"unknown tool: {tool}. Use edit with content to create or replace a file, "
                'for example {"tool":"edit","args":{"path":"app.py","content":"..."}}.'
            )

        call = self._tool_call(tool, args)
        if call is None:
            return ToolPlan(calls=[], control=None)
        return ToolPlan(calls=[call], control=Control(kind="continue", body="Need tool result"))

    def _read_files(self, args: dict[str, Any]) -> list[ToolCall]:
        paths = args.get("paths")
        if isinstance(paths, str):
            paths = [paths]
        if not isinstance(paths, list) or not paths:
            raise ProtocolValidationError("read_files requires a non-empty paths list")
        if len(paths) > MAX_ACCIDENTAL_TOOL_CALLS:
            raise ProtocolValidationError(
                f"read_files accepts at most {MAX_ACCIDENTAL_TOOL_CALLS} paths"
            )
        calls = []
        for path in paths:
            if not path:
                raise ProtocolValidationError("read_files paths cannot be empty")
            calls.append(ToolCall(name="read", args={"path": str(path)}))
        return calls

    def _parallel(self, args: dict[str, Any]) -> list[ToolCall]:
        raw_calls = args.get("calls")
        if not isinstance(raw_calls, list) or not raw_calls:
            raise ProtocolValidationError("parallel requires a non-empty calls list")
        if len(raw_calls) > MAX_PARALLEL_CALLS:
            raise ProtocolValidationError(
                f"parallel accepts at most {MAX_PARALLEL_CALLS} read-only calls"
            )

        validated: list[tuple[str, dict[str, Any]]] = []
        for raw in raw_calls:
            if not isinstance(raw, dict):
                raise ProtocolValidationError("every parallel call must be an object")
            tool = str(raw.get("tool") or raw.get("name") or "").lower().strip()
            spec = TOOL_SPEC_BY_NAME.get(tool)
            if spec is None or not spec.parallel_safe:
                raise ProtocolValidationError(
                    "parallel accepts read-only list_dir, read_file, and grep calls only"
                )
            validated.append((tool, _object_args(raw)))

        calls: list[ToolCall] = []
        for tool, raw_args in validated:
            call = self._tool_call(tool, raw_args)
            if call is None:
                raise ProtocolValidationError(f"invalid {tool} call inside parallel")
            calls.append(call)
        return calls

    def _tool_call(self, tool: str, args: dict[str, Any]) -> ToolCall | None:
        spec = TOOL_SPEC_BY_NAME.get(tool.lower().strip())
        if spec is None or spec.runtime_name is None:
            return None
        normalized = spec.runtime_name

        path = _text(args.get("path") or args.get("cwd"), ".").strip() or "."
        call_args: dict[str, Any] = {"path": path}

        if normalized == "edit":
            if "path" not in args and "cwd" not in args:
                raise ProtocolValidationError(
                    "edit requires a top-level path and can only edit one file"
                )
            has_content = "content" in args
            has_replacements = "replacements" in args
            has_single = any(
                name in args
                for name in ("old_string", "new_string", "search", "replace", "replacement")
            )
            if sum((has_content, has_replacements, has_single)) != 1:
                raise ProtocolValidationError(
                    "edit requires exactly one mode: content, old_string/new_string, "
                    "or replacements"
                )
            if has_content:
                call_args["content"] = _text(args.get("content"))
            elif has_replacements:
                replacements = args.get("replacements")
                if not isinstance(replacements, list) or not replacements:
                    raise ProtocolValidationError(
                        "edit replacements must be a non-empty list"
                    )
                if len(replacements) > MAX_REPLACEMENTS:
                    raise ProtocolValidationError(
                        f"edit supports at most {MAX_REPLACEMENTS} replacements"
                    )
                normalized_replacements: list[dict[str, str]] = []
                for item in replacements:
                    if not isinstance(item, dict):
                        raise ProtocolValidationError(
                            "every edit replacement must be an object"
                        )
                    if "path" in item or "cwd" in item:
                        raise ProtocolValidationError(
                            "edit replacements apply to the top-level path only; "
                            "use separate edit calls for different files"
                        )
                    old = item.get("old_string", item.get("search"))
                    if "new_string" in item:
                        new = item.get("new_string")
                    elif "replace" in item:
                        new = item.get("replace")
                    else:
                        new = item.get("replacement")
                    if old is None or new is None or not str(old):
                        raise ProtocolValidationError(
                            "every edit replacement requires non-empty old_string "
                            "and a new_string"
                        )
                    normalized_replacements.append({
                        "search": _text(old),
                        "replace": _text(new),
                    })
                call_args["replacements"] = normalized_replacements
            else:
                old = args.get("old_string")
                new = args.get("new_string")
                if old is None:
                    old = args.get("search")
                if new is None:
                    new = args.get("replace", args.get("replacement"))
                if old is None or new is None or not str(old):
                    raise ProtocolValidationError(
                        "edit requires non-empty old_string and a new_string"
                    )
                call_args["replacements"] = [{"search": _text(old), "replace": _text(new)}]
        elif normalized == "read":
            if "offset" in args:
                call_args["offset"] = _positive_int_arg(args, "offset", 1)
            if "limit" in args:
                call_args["limit"] = _positive_int_arg(
                    args,
                    "limit",
                    READ_DEFAULT_LINES,
                    READ_MAX_LINES,
                )
        elif normalized == "search":
            pattern = args.get("pattern", args.get("query"))
            if not pattern:
                raise ProtocolValidationError("grep requires a pattern")
            call_args["query"] = _text(pattern)
        elif normalized == "references":
            symbol = args.get("symbol", args.get("name"))
            if not symbol:
                raise ProtocolValidationError("find_references requires a symbol")
            call_args["symbol"] = _text(symbol).strip()
        elif normalized in {"run", "shell"}:
            command = args.get("command", args.get("cmd"))
            if not command:
                raise ProtocolValidationError(f"{spec.name} requires a command")
            call_args["command"] = _text(command)

        return ToolCall(name=normalized, args=call_args)
