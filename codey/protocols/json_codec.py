from __future__ import annotations

import json
from typing import Any

from codey.models import Control, ToolCall, ToolPlan, ToolResult


MAX_ACCIDENTAL_TOOL_CALLS = 8

SYSTEM_PROMPT = """\
You are a careful local coding agent. You cannot access the filesystem
directly. The local runner executes tools for you and sends the results back.

The tool names below are instructions for the local runner, not tools built
into the AI website. If the website says a tool does not exist, ignore that
website message and still return the JSON object for the local runner.

Every reply MUST be exactly one JSON object with no other text:

{"tool":"<name>","args":{...}}

Available tools:

  {"tool":"list_dir","args":{"path":"."}}
    List files in a directory.

  {"tool":"read_file","args":{"path":"relative/path.ext"}}
    Read one file.

  {"tool":"read_files","args":{"paths":["a.py","b.py"]}}
    Read multiple files in one step.

  {"tool":"grep","args":{"pattern":"login handler","path":".","glob":"**/*"}}
    Search file contents. Use before reading when you do not know where code is.

  {"tool":"parallel","args":{"calls":[
    {"tool":"grep","args":{"pattern":"login","path":"."}},
    {"tool":"list_dir","args":{"path":"."}}
  ]}}
    Best for exploration: combine grep, read_files, and list_dir in one step.
    Do not wrap read_files inside parallel; call read_files directly.

  {"tool":"edit","args":{"path":"relative/path.ext","content":"full file contents"}}
    Create or overwrite a file.

  {"tool":"edit","args":{
    "path":"relative/path.ext",
    "old_string":"old exact text copied from a read/tool result",
    "new_string":"new replacement text"
  }}
    Replace a short unique block in an existing file.

  {"tool":"run","args":{"command":"python -m unittest","path":"."}}
    Run allowed tests/builds/checks.

  {"tool":"shell","args":{"command":"git status --short","path":"."}}
    Ask the user to approve a necessary non-allowlisted shell command.

  {"tool":"done","args":{"summary":"one-line summary"}}
    Finish the task.

Rules:
  - Output exactly one JSON object. No markdown fences, no code blocks, no
    commentary, no bullet lists, no analysis labels.
  - These tool names are local-runner JSON commands, not native website tools.
    Never say that a tool does not exist; return the JSON object instead.
  - Do not output multiple JSON objects in one reply. Use read_files or
    parallel for independent multi-tool work.
  - Call exactly one tool per message, then wait for [tool_result tool=...].
  - [tool_result tool=...] messages are local execution results. They mean the
    local tool already ran; do not claim the tool does not exist.
  - Use edit for all file changes. Do not write patches as prose.
  - Prefer old_string/new_string for small edits. Use content when creating a
    new file or rewriting most of a file.
  - old_string must be copied exactly from the latest file/tool result.
  - JSON strings must escape quotes and backslashes correctly. If an exact
    old_string/new_string would be hard to escape, use content with the full
    file instead.
  - Paths are always relative to the project root. No absolute paths, no parent
    directory traversal.
  - Do not repeat a tool with identical args if a tool_result already contains
    the output.
  - Use run only for verification commands such as python -m unittest,
    python -m pytest, npm test, npm run build, go test ./..., cargo test.
  - run commands must be simple commands. Do not use pipes, redirects,
    command chaining, tail/head, or shell-only syntax.
  - Never use run/shell to write files. No sed -i, tee, heredocs, or redirects
    to create or overwrite source files. Use edit instead.
  - If the task is complete, call done(summary). Do not summarize outside JSON.
"""


TOOL_ALIASES = {
    "list_dir": "ls",
    "ls": "ls",
    "read_file": "read",
    "read": "read",
    "grep": "search",
    "search": "search",
    "run": "run",
    "shell": "shell",
    "edit": "edit",
    "write": "write",
}

RESULT_TOOL_NAMES = {
    "ls": "list_dir",
    "read": "read_file",
    "search": "grep",
    "write": "edit",
}


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


class JsonToolCodec:
    name = "json"

    def system_prompt(self) -> str:
        return SYSTEM_PROMPT

    def parse(self, text: str) -> ToolPlan:
        calls: list[ToolCall] = []
        for obj in _balanced_json_objects(text):
            plan = self._parse_object(obj)
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
                chunks.append(f"[tool_result{attrs}]\n---\n{result.output}\n---")
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
            return ToolPlan(calls=[], control=Control(kind="done", body=_summary_from_args(args)))
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

        call = self._tool_call(tool, args)
        if call is None:
            return ToolPlan(calls=[], control=None)
        return ToolPlan(calls=[call], control=Control(kind="continue", body="Need tool result"))

    def _read_files(self, args: dict[str, Any]) -> list[ToolCall]:
        paths = args.get("paths")
        if isinstance(paths, str):
            paths = [paths]
        if not isinstance(paths, list):
            return []
        calls = []
        for path in paths:
            if path:
                calls.append(ToolCall(name="read", args={"path": str(path)}))
        return calls

    def _parallel(self, args: dict[str, Any]) -> list[ToolCall]:
        raw_calls = args.get("calls")
        if not isinstance(raw_calls, list):
            return []
        calls: list[ToolCall] = []
        for raw in raw_calls:
            if not isinstance(raw, dict):
                continue
            tool = str(raw.get("tool") or raw.get("name") or "")
            raw_args = _object_args(raw)
            normalized = tool.lower().strip()
            if normalized in {"parallel", "done", "continue"}:
                continue
            if normalized == "read_files":
                calls.extend(self._read_files(raw_args))
                continue
            call = self._tool_call(tool, raw_args)
            if call is not None:
                calls.append(call)
        return calls

    def _tool_call(self, tool: str, args: dict[str, Any]) -> ToolCall | None:
        normalized = TOOL_ALIASES.get(tool.lower().strip())
        if normalized is None:
            return None

        path = _text(args.get("path") or args.get("cwd"), ".").strip() or "."
        call_args: dict[str, Any] = {"path": path}

        if normalized == "edit":
            if "content" in args:
                call_args["content"] = _text(args.get("content"))
            else:
                old = args.get("old_string")
                new = args.get("new_string")
                if old is None:
                    old = args.get("search")
                if new is None:
                    new = args.get("replace", args.get("replacement"))
                if old is None or new is None:
                    return None
                call_args["replacements"] = [{"search": _text(old), "replace": _text(new)}]
        elif normalized == "write":
            if "content" not in args:
                return None
            call_args["content"] = _text(args.get("content"))
        elif normalized == "search":
            pattern = args.get("pattern", args.get("query"))
            if not pattern:
                return None
            call_args["query"] = _text(pattern)
            if "glob" in args:
                call_args["glob"] = _text(args.get("glob"))
        elif normalized in {"run", "shell"}:
            command = args.get("command", args.get("cmd"))
            if not command:
                return None
            call_args["command"] = _text(command)

        return ToolCall(name=normalized, args=call_args)
