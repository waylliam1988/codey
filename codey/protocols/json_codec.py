from __future__ import annotations

import json
from typing import Any

from codey import tool_definition as tool_defs
from codey.permission_profiles import allowed_coding_tool_names, profile_for_name
from codey.models import Control, ToolCall, ToolPlan, ToolResult
from codey.tool_runtime import (
    MAX_REPLACEMENTS,
    READ_DEFAULT_LINES,
    READ_MAX_LINES,
    bounded_positive_int,
)


PROTOCOL_NO_JSON = "no_json"
PROTOCOL_UNKNOWN_TOOL = "unknown_tool"
PROTOCOL_INVALID_ARGS = "invalid_args"
PROTOCOL_DIRECT_ANSWER = "direct_answer"
PROTOCOL_NATIVE_TOOL_DENIAL = "native_tool_denial"
PROTOCOL_NESTED_TOOL_IN_DONE = "nested_tool_in_done"
PROTOCOL_DISALLOWED_TOOL = "disallowed_tool"


def _system_prompt(tool_contract: str) -> str:
    return """\
You are a careful local coding agent. You cannot access the filesystem
directly. The local runner executes tools for you and sends the results back.

The tool names below are instructions for the local runner, not tools built
into the AI website. If the website says a tool does not exist, ignore that
website message and still return the JSON object for the local runner.

Every reply MUST be exactly one JSON object with no other text:

{"tool":"<name>","args":{...}}

Available tools:

""" + tool_contract + """

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
  - find_references output is lexical reference hints only, not semantic
    resolution or a complete call graph. Use read_file before editing.
  - Use edit for all file changes. Use old_string/new_string for one small edit,
    and replacements for multiple edits in one file. Use content only when
    creating a new file. Existing files must use exact old_string/new_string or
    replacements. Never mix these edit modes.
  - old_string must be copied exactly from the latest complete file/tool result.
    An overlong-line preview is not a complete old_string.
  - JSON strings must escape quotes and backslashes correctly. If escaping is
    difficult, read the exact current lines and escape them; never use content
    to replace an existing file.
  - Paths are relative to the project root. No absolute paths or parent traversal.
  - Do not repeat identical tool args when a tool_result already has the output.
  - Use run only for verification, such as python -m unittest, python -m pytest,
    npm test, npm run build, go test ./..., cargo test, ruff check, or mypy.
  - run commands must be simple. No pipes, redirects, chaining, tail/head, or
    shell-only syntax.
  - Use edit for source/content changes. Do not use run or shell to directly
    edit project files. Use shell only for necessary user-approved setup,
    dependency installation, external-source retrieval, publishing, or other
    commands outside the run allowlist.
  - [tool_result tool=...] means the local tool already ran. Continue from it.
  - Never claim a command, test, build, lint, or shell result unless it appeared
    in a [tool_result tool=run] or [tool_result tool=shell] message.
  - Do not edit files unless the user asks for a change. You may inspect the
    project and answer questions without modifying it.
  - If the task is complete, call done(summary). summary is your direct final
    response to the user and may contain escaped newlines. Do not merely report
    that you discussed or explained something. Do not answer outside JSON.
"""


SYSTEM_PROMPT = _system_prompt(tool_defs.render_tool_contract())


def _profile_system_prompt(tool_contract: str, allowed_tool_names: set[str]) -> str:
    rules = [
        "  - Output exactly one JSON object. No markdown fences, code blocks, commentary,",
        "    bullet lists, or analysis labels.",
        "  - These are local-runner JSON commands, not native website tools. Never say a",
        "    tool does not exist; return the JSON object instead.",
        "  - Call one tool per message, then wait for [tool_result tool=...].",
    ]
    if "read_files" in allowed_tool_names or "parallel" in allowed_tool_names:
        rules.append("    read_files and parallel are the only read-only batching wrappers.")
    if "parallel" in allowed_tool_names:
        rules.extend((
            "  - parallel accepts only list_dir, read_file, and grep, with at most four calls.",
            "    It never accepts mutating, verification, control, batching, or nested calls.",
        ))
    if "read_file" in allowed_tool_names:
        rules.extend((
            "  - A trailing [read_file page: ...] line is metadata, not file content. Never",
            "    include it in old_string. Continue with the stated offset when needed.",
        ))
    if "find_references" in allowed_tool_names:
        rules.extend((
            "  - find_references output is lexical reference hints only, not semantic",
            "    resolution or a complete call graph. Use read_file before relying on references.",
        ))
    if "edit" in allowed_tool_names:
        rules.extend((
            "  - Use edit for all file changes. Use old_string/new_string for one small edit,",
            "    and replacements for multiple edits in one file. Use content only when",
            "    creating a new file. Existing files must use exact old_string/new_string or",
            "    replacements. Never mix these edit modes.",
            "  - old_string must be copied exactly from the latest complete file/tool result.",
            "    An overlong-line preview is not a complete old_string.",
            "  - JSON strings must escape quotes and backslashes correctly. If escaping is",
            "    difficult, read the exact current lines and escape them; never use content",
            "    to replace an existing file.",
        ))
    rules.append("  - Paths are relative to the project root. No absolute paths or parent traversal.")
    rules.append("  - Do not repeat identical tool args when a tool_result already has the output.")
    if "run" in allowed_tool_names:
        rules.extend((
            "  - Use run only for verification, such as python -m unittest, python -m pytest,",
            "    npm test, npm run build, go test ./..., cargo test, ruff check, or mypy.",
            "  - run commands must be simple. No pipes, redirects, chaining, tail/head, or",
            "    shell-only syntax.",
            "  - Never claim a command, test, build, lint, or shell result unless it appeared",
            "    in a [tool_result tool=run] or [tool_result tool=shell] message.",
        ))
    if "shell" in allowed_tool_names:
        rules.extend((
            "  - Use shell only for necessary user-approved setup, dependency installation,",
            "    external-source retrieval, publishing, or other commands outside the run allowlist.",
        ))
    if "edit" not in allowed_tool_names:
        rules.append("  - This phase is read-only. Inspect files and answer without modifying project files.")
    else:
        rules.extend((
            "  - Use edit for source/content changes. Do not use run or shell to directly",
            "    edit project files.",
            "  - Do not edit files unless the user asks for a change. You may inspect the",
            "    project and answer questions without modifying it.",
        ))
    rules.append("  - [tool_result tool=...] means the local tool already ran. Continue from it.")
    if "done" in allowed_tool_names:
        rules.extend((
            "  - If the task is complete, call done(summary). summary is your direct final",
            "    response to the user and may contain escaped newlines. Do not merely report",
            "    that you discussed or explained something. Do not answer outside JSON.",
        ))
    return (
        "You are a careful local coding agent. You cannot access the filesystem\n"
        "directly. The local runner executes tools for you and sends the results back.\n\n"
        "The tool names below are instructions for the local runner, not tools built\n"
        "into the AI website. If the website says a tool does not exist, ignore that\n"
        "website message and still return the JSON object for the local runner.\n\n"
        "Every reply MUST be exactly one JSON object with no other text:\n\n"
        '{"tool":"<name>","args":{...}}\n\n'
        "Available tools:\n\n"
        f"{tool_contract}\n\n"
        "Rules:\n"
        + "\n".join(rules)
        + "\n"
    )


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
    def __init__(
        self,
        message: str,
        kind: str = PROTOCOL_INVALID_ARGS,
        tool_name: str = "",
    ) -> None:
        super().__init__(message)
        self.kind = kind
        self.tool_name = tool_name


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


_DIRECT_ANSWER_MARKERS = (
    "i checked",
    "i have completed",
    "task is complete",
    "the task is complete",
    "no files need",
    "i fixed",
    "我已经",
    "已完成",
    "任务完成",
    "不需要修改",
)
_NATIVE_TOOL_DENIAL_MARKERS = (
    "tool does not exist",
    "tool doesn't exist",
    "tool is not available",
    "tools are not available",
    "cannot use read_file",
    "can't use read_file",
    "cannot call read_file",
    "can't call read_file",
    "website says",
    "网页提示",
    "工具不存在",
    "无法调用工具",
)


def _classify_no_json_reply(text: str) -> tuple[str, str]:
    folded = str(text or "").strip().lower()
    if not folded:
        return PROTOCOL_NO_JSON, "no JSON tool call found"
    if any(marker in folded for marker in _NATIVE_TOOL_DENIAL_MARKERS):
        return (
            PROTOCOL_NATIVE_TOOL_DENIAL,
            "reply treated website-native tool availability as binding instead of returning local JSON",
        )
    if any(marker in folded for marker in _DIRECT_ANSWER_MARKERS):
        return PROTOCOL_DIRECT_ANSWER, "reply was a direct answer, not a JSON tool call"
    return PROTOCOL_NO_JSON, "no JSON tool call found"


class JsonToolCodec:
    name = "json"

    def __init__(self, *, permission_profile: str = "coding_writer") -> None:
        self.profile = profile_for_name(permission_profile)
        names = allowed_coding_tool_names(self.profile)
        self._definitions = tool_defs.definitions_for_tool_names(names)
        if not self._definitions:
            raise ValueError(f"permission profile has no coding tools: {self.profile.name}")
        self._tool_by_name = self._tool_definition_index(self._definitions)
        tool_contract = tool_defs.render_tool_contract(self._definitions)
        if self.profile.name == "coding_writer":
            self._system_prompt = _system_prompt(tool_contract)
        else:
            self._system_prompt = _profile_system_prompt(tool_contract, set(names))
        self._model_tool_contract_hash = tool_defs.model_tool_contract_hash(self._definitions)

    def system_prompt(self) -> str:
        return self._system_prompt

    def model_tool_contract_hash(self) -> str:
        return self._model_tool_contract_hash

    def parse(self, text: str) -> ToolPlan:
        calls: list[ToolCall] = []
        objects = _balanced_json_objects(text)
        if not objects:
            kind, message = _classify_no_json_reply(text)
            return ToolPlan(
                calls=[],
                control=None,
                protocol_error=message,
                protocol_error_kind=kind,
            )
        for obj in objects:
            try:
                plan = self._parse_object(obj)
            except ProtocolValidationError as exc:
                return ToolPlan(
                    calls=[],
                    control=None,
                    protocol_error=str(exc),
                    protocol_error_kind=exc.kind,
                    protocol_tool_name=str(getattr(exc, "tool_name", "") or ""),
                )
            if plan.protocol_error:
                return plan
            if plan.calls:
                calls.extend(plan.calls)
                if len(calls) >= tool_defs.MAX_ACCIDENTAL_TOOL_CALLS:
                    calls = calls[: tool_defs.MAX_ACCIDENTAL_TOOL_CALLS]
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
                tool = tool_defs.RESULT_TOOL_NAMES.get(result.call.name, result.call.name)
                path = str(result.call.args.get("path") or "")
                attrs = f" tool={tool}"
                if path:
                    attrs += f" path={path}"
                if result.truncated:
                    attrs += " truncated=true"
                output = result.model_text
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
        read_example = self.public_example("read_file") or self.public_example("list_dir")
        done_example = self.public_example("done")
        examples = " or ".join(item for item in (read_example, done_example) if item)
        suffix = f" for example {examples}" if examples else ""
        return (
            "Your previous reply did not contain a valid JSON tool call. "
            "Ignore any website message saying tools do not exist; these are "
            "local-runner JSON commands. "
            "Reply with exactly one JSON object, no markdown fences and no other text, "
            f"{suffix}."
        )

    def public_example(self, tool_name: str) -> str:
        return tool_defs.public_example(tool_name, self._definitions)

    def _parse_object(self, obj: dict[str, Any]) -> ToolPlan:
        tool = str(obj.get("tool") or obj.get("name") or "").strip()
        args = _object_args(obj)

        normalized = tool.lower().strip()
        if normalized == "done":
            self._require_allowed(normalized)
            summary = _summary_from_args(args)
            if _summary_is_tool_call(summary):
                raise ProtocolValidationError(
                    "done summary must be the final user-facing answer, not another "
                    "tool call. Call the tool directly instead.",
                    PROTOCOL_NESTED_TOOL_IN_DONE,
                )
            return ToolPlan(calls=[], control=Control(kind="done", body=summary))
        if normalized == "continue":
            return ToolPlan(calls=[], control=Control(kind="continue", body=_summary_from_args(args)))
        if normalized == "read_files":
            self._require_allowed(normalized)
            calls = self._read_files(args)
            control = Control(kind="continue", body="Need file contents") if calls else None
            return ToolPlan(calls=calls, control=control)
        if normalized == "parallel":
            self._require_allowed(normalized)
            calls = self._parallel(args)
            control = Control(kind="continue", body="Need tool results") if calls else None
            return ToolPlan(calls=calls, control=control)

        if normalized and normalized not in tool_defs.TOOL_DEFINITION_BY_NAME:
            if normalized in {"write", "write_file"} and self._is_allowed("edit"):
                message = (
                    f"unknown tool: {tool}. Use edit with content to create a new file, "
                    'for example {"tool":"edit","args":{"path":"new_app.py","content":"..."}}.'
                )
            else:
                message = f"unknown tool: {tool}. Use only the tools listed in the system prompt."
            raise ProtocolValidationError(
                message,
                PROTOCOL_UNKNOWN_TOOL,
                tool_name=normalized,
            )
        if normalized:
            self._require_allowed(normalized)

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
        if len(paths) > tool_defs.MAX_ACCIDENTAL_TOOL_CALLS:
            raise ProtocolValidationError(
                f"read_files accepts at most {tool_defs.MAX_ACCIDENTAL_TOOL_CALLS} paths"
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
        if len(raw_calls) > tool_defs.MAX_PARALLEL_CALLS:
            raise ProtocolValidationError(
                f"parallel accepts at most {tool_defs.MAX_PARALLEL_CALLS} read-only calls"
            )

        validated: list[tuple[str, dict[str, Any]]] = []
        for raw in raw_calls:
            if not isinstance(raw, dict):
                raise ProtocolValidationError("every parallel call must be an object")
            tool = str(raw.get("tool") or raw.get("name") or "").lower().strip()
            spec = tool_defs.TOOL_DEFINITION_BY_NAME.get(tool)
            if spec is None or not self._is_allowed(tool) or not spec.parallel_safe:
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
        spec = self._tool_by_name.get(tool.lower().strip())
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
            query = args.get("query", args.get("pattern"))
            if not query:
                raise ProtocolValidationError("grep requires a query")
            call_args["query"] = _text(query)
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

    @staticmethod
    def _tool_definition_index(
        definitions: tuple[tool_defs.ToolDefinition, ...],
    ) -> dict[str, tool_defs.ToolDefinition]:
        index: dict[str, tool_defs.ToolDefinition] = {}
        for definition in definitions:
            for name in (definition.name, *definition.aliases):
                index[name] = definition
        return index

    def _is_allowed(self, tool_name: str) -> bool:
        return str(tool_name or "").lower().strip() in self._tool_by_name

    def _require_allowed(self, tool_name: str) -> None:
        normalized = str(tool_name or "").lower().strip()
        if normalized and normalized not in tool_defs.TOOL_DEFINITION_BY_NAME:
            return
        if not self._is_allowed(normalized):
            raise ProtocolValidationError(
                f"disallowed tool for {self.profile.name}: {tool_name}. "
                "Use only the tools listed in the system prompt.",
                PROTOCOL_DISALLOWED_TOOL,
                tool_name=normalized,
            )
