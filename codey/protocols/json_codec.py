from __future__ import annotations

import json
from typing import Any

from codey.tool_prompt import (
    coding_model_tool_contract_hash,
    render_coding_system_prompt,
)
from codey.toolchain import definition as tool_defs
from codey.policies.permissions import allowed_coding_tool_names, profile_for_name
from codey.runtime.models import Control, ToolCall, ToolPlan, ToolResult
from codey.tool_args_repair import (
    ToolArgLimits,
    ToolArgsRepairError,
    ToolArgsRepairResult,
    normalize_tool_args,
)
from codey.toolchain.runtime import (
    MAX_REPLACEMENTS,
    READ_MAX_LINES,
)


_DEFAULT_TOOL_ARG_LIMITS = ToolArgLimits(
    max_replacements=MAX_REPLACEMENTS,
    read_max_lines=READ_MAX_LINES,
)


PROTOCOL_NO_JSON = "no_json"
PROTOCOL_UNKNOWN_TOOL = "unknown_tool"
PROTOCOL_INVALID_ARGS = "invalid_args"
PROTOCOL_DIRECT_ANSWER = "direct_answer"
PROTOCOL_NATIVE_TOOL_DENIAL = "native_tool_denial"
PROTOCOL_NESTED_TOOL_IN_DONE = "nested_tool_in_done"
PROTOCOL_DISALLOWED_TOOL = "disallowed_tool"


def _strip_think_blocks(text: str) -> str:
    source = str(text or "")
    lowered = source.lower()
    output: list[str] = []
    index = 0
    depth = 0
    in_string = False
    escaped = False
    while index < len(source):
        if depth == 0 and lowered.startswith("<think", index):
            tag_end = lowered.find(">", index)
            if tag_end < 0:
                break
            end = lowered.find("</think>", tag_end + 1)
            if end < 0:
                break
            index = end + len("</think>")
            continue

        char = source[index]
        output.append(char)
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
        elif char == "{":
            depth += 1
        elif char == "}" and depth:
            depth -= 1
        elif char == '"' and depth:
            in_string = True
        index += 1
    return "".join(output)


def _tool_call_key(call: ToolCall) -> tuple[str, str]:
    args = json.dumps(
        call.args,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return call.name, args


SYSTEM_PROMPT = render_coding_system_prompt(
    tool_defs.TOOL_DEFINITIONS,
    profile_name="coding_writer",
    allowed_tool_names={definition.name for definition in tool_defs.TOOL_DEFINITIONS},
)


def _balanced_json_objects(text: str) -> list[dict[str, Any]]:
    """Extract JSON objects from raw web replies without accepting prose."""
    text = _strip_think_blocks(text)
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


def _object_args(obj: dict[str, Any]) -> dict[str, Any]:
    if "args" in obj:
        args = obj.get("args")
        return args if isinstance(args, dict) else {}
    return {key: value for key, value in obj.items() if key not in {"tool", "name"}}


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
        self._system_prompt = render_coding_system_prompt(
            self._definitions,
            profile_name=self.profile.name,
            allowed_tool_names=set(names),
        )
        self._model_tool_contract_hash = coding_model_tool_contract_hash(self._definitions)

    def system_prompt(self) -> str:
        return self._system_prompt

    def model_tool_contract_hash(self) -> str:
        return self._model_tool_contract_hash

    def parse(self, text: str) -> ToolPlan:
        calls: list[ToolCall] = []
        seen_calls: set[tuple[str, str]] = set()
        total_rewrites = 0
        merged_repair_counts: dict[str, int] = {}
        objects = _balanced_json_objects(text)
        if not objects:
            kind, message = _classify_no_json_reply(_strip_think_blocks(text))
            return ToolPlan(
                calls=[],
                control=None,
                protocol_error=message,
                protocol_error_kind=kind,
            )
        for obj in objects:
            try:
                pairs, control = self._parse_object_items(obj)
            except ProtocolValidationError as exc:
                return ToolPlan(
                    calls=[],
                    control=None,
                    protocol_error=str(exc),
                    protocol_error_kind=exc.kind,
                    protocol_tool_name=str(getattr(exc, "tool_name", "") or ""),
                )
            if pairs:
                for call, repair in pairs:
                    key = _tool_call_key(call)
                    if key in seen_calls:
                        continue
                    seen_calls.add(key)
                    calls.append(call)
                    total_rewrites += repair.alias_rewrite_count
                    for k, v in repair.arg_repair_counts.items():
                        merged_repair_counts[k] = merged_repair_counts.get(k, 0) + v
                    if len(calls) >= tool_defs.MAX_ACCIDENTAL_TOOL_CALLS:
                        break
                if len(calls) >= tool_defs.MAX_ACCIDENTAL_TOOL_CALLS:
                    break
                continue
            if calls:
                continue
            if control is not None:
                return ToolPlan(
                    calls=[],
                    control=control,
                    alias_rewrite_count=total_rewrites,
                    arg_repair_counts=merged_repair_counts,
                )
        if calls:
            body = "Need tool result" if len(calls) == 1 else "Need tool results"
            return ToolPlan(
                calls=calls,
                control=Control(kind="continue", body=body),
                alias_rewrite_count=total_rewrites,
                arg_repair_counts=merged_repair_counts,
            )
        return ToolPlan(
            calls=[],
            control=None,
            alias_rewrite_count=total_rewrites,
            arg_repair_counts=merged_repair_counts,
        )

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

    def _parse_object_items(
        self,
        obj: dict[str, Any],
    ) -> tuple[list[tuple[ToolCall, ToolArgsRepairResult]], Control | None]:
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
            return [], Control(kind="done", body=summary)
        if normalized == "continue":
            return [], Control(kind="continue", body=_summary_from_args(args))
        if normalized == "read_files":
            self._require_allowed(normalized)
            pairs = self._read_files(args)
            control = Control(kind="continue", body="Need file contents") if pairs else None
            return pairs, control
        if normalized == "parallel":
            self._require_allowed(normalized)
            pairs = self._parallel(args)
            control = Control(kind="continue", body="Need tool results") if pairs else None
            return pairs, control

        if normalized and normalized not in tool_defs.TOOL_DEFINITION_BY_NAME:
            if normalized in {"write", "write_file", "create_file"} and self._is_allowed("edit"):
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

        call, repair = self._tool_call(tool, args)
        if call is None:
            return [], None
        return [(call, repair)], Control(kind="continue", body="Need tool result")

    def _read_files(self, args: dict[str, Any]) -> list[tuple[ToolCall, ToolArgsRepairResult]]:
        paths = args.get("paths")
        if isinstance(paths, str):
            paths = [paths]
        if not isinstance(paths, list) or not paths:
            raise ProtocolValidationError("read_files requires a non-empty paths list")
        if len(paths) > tool_defs.MAX_ACCIDENTAL_TOOL_CALLS:
            raise ProtocolValidationError(
                f"read_files accepts at most {tool_defs.MAX_ACCIDENTAL_TOOL_CALLS} paths"
            )
        pairs: list[tuple[ToolCall, ToolArgsRepairResult]] = []
        for raw_path in paths:
            if not raw_path:
                raise ProtocolValidationError("read_files paths cannot be empty")
            try:
                repair = normalize_tool_args(
                    "read",
                    {"path": raw_path},
                    limits=_DEFAULT_TOOL_ARG_LIMITS,
                )
            except ToolArgsRepairError as exc:
                raise ProtocolValidationError(str(exc), kind=exc.repair_kind, tool_name="read") from exc
            pairs.append((ToolCall(name="read", args=repair.args), repair))
        return pairs

    def _parallel(self, args: dict[str, Any]) -> list[tuple[ToolCall, ToolArgsRepairResult]]:
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

        pairs: list[tuple[ToolCall, ToolArgsRepairResult]] = []
        for tool, raw_args in validated:
            call, repair = self._tool_call(tool, raw_args)
            if call is None:
                raise ProtocolValidationError(f"invalid {tool} call inside parallel")
            pairs.append((call, repair))
        return pairs

    def _tool_call(
        self,
        tool: str,
        args: dict[str, Any],
    ) -> tuple[ToolCall | None, ToolArgsRepairResult]:
        spec = self._tool_by_name.get(tool.lower().strip())
        if spec is None or spec.runtime_name is None:
            return None, ToolArgsRepairResult(args={})
        normalized = spec.runtime_name

        try:
            repair = normalize_tool_args(
                normalized,
                args,
                limits=_DEFAULT_TOOL_ARG_LIMITS,
            )
        except ToolArgsRepairError as exc:
            raise ProtocolValidationError(
                str(exc),
                kind=exc.repair_kind,
                tool_name=normalized,
            ) from exc

        return ToolCall(name=normalized, args=repair.args), repair

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
