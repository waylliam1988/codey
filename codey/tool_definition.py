"""Internal coding tool definitions shared by protocol and agent runtime."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass

from codey.models import ToolCall
from codey.tool_runtime import MAX_REPLACEMENTS


MAX_ACCIDENTAL_TOOL_CALLS = 8
MAX_PARALLEL_CALLS = 4


@dataclass(frozen=True)
class ToolDefinition:
    name: str
    runtime_name: str | None
    aliases: tuple[str, ...] = ()
    read_only: bool = False
    parallel_safe: bool = False
    permission: str = ""
    examples: tuple[str, ...] = ()
    description: str = ""
    output_facts: tuple[str, ...] = ()
    render_hint: str = ""
    repair_hint: str = ""


TOOL_DEFINITIONS = (
    ToolDefinition(
        "list_dir",
        "ls",
        aliases=("ls",),
        read_only=True,
        parallel_safe=True,
        permission="project_read",
        examples=('{"tool":"list_dir","args":{"path":"."}}',),
        description="List files in a directory.",
        render_hint="list",
        repair_hint="list_dir",
    ),
    ToolDefinition(
        "read_file",
        "read",
        aliases=("read",),
        read_only=True,
        parallel_safe=True,
        permission="project_read",
        examples=(
            '{"tool":"read_file","args":{"path":"app.py"}}',
            '{"tool":"read_file","args":{"path":"app.py","offset":301,"limit":300}}',
        ),
        description="Read one file. Large files are returned in complete-line pages.",
        render_hint="read",
        repair_hint="read_file",
    ),
    ToolDefinition(
        "read_files",
        None,
        read_only=True,
        permission="project_read",
        examples=('{"tool":"read_files","args":{"paths":["a.py","b.py"]}}',),
        description=(
            f"Read up to {MAX_ACCIDENTAL_TOOL_CALLS} files in one step. "
            "Do not nest it inside parallel."
        ),
        render_hint="read_many",
        repair_hint="read_files",
    ),
    ToolDefinition(
        "grep",
        "search",
        aliases=("search",),
        read_only=True,
        parallel_safe=True,
        permission="project_read",
        examples=(
            '{"tool":"grep","args":{"query":"login handler","path":"."}}',
        ),
        description=(
            "Search file contents for literal text before reading when the location "
            "is unknown. Matching is case-insensitive; regex is not supported."
        ),
        render_hint="search",
        repair_hint="grep",
    ),
    ToolDefinition(
        "find_references",
        "references",
        aliases=("references",),
        read_only=True,
        permission="project_read",
        examples=(
            '{"tool":"find_references","args":{"symbol":"createRouter","path":"."}}',
        ),
        description=(
            "Find bounded lexical reference hints for a simple symbol. "
            "This is not semantic resolution; use read_file before editing."
        ),
        render_hint="references",
        repair_hint="find_references",
    ),
    ToolDefinition(
        "parallel",
        None,
        read_only=True,
        permission="project_read",
        examples=(
            '{"tool":"parallel","args":{"calls":[{"tool":"grep","args":{"query":"login","path":"."}},{"tool":"list_dir","args":{"path":"."}}]}}',
        ),
        description=(
            f"Batch at most {MAX_PARALLEL_CALLS} independent read-only list_dir, "
            "read_file, or grep calls. Local results are returned in request order."
        ),
        render_hint="parallel",
        repair_hint="parallel",
    ),
    ToolDefinition(
        "edit",
        "edit",
        permission="project_write",
        examples=(
            '{"tool":"edit","args":{"path":"new_app.py","content":"full file contents"}}',
            '{"tool":"edit","args":{"path":"app.py","old_string":"old exact text","new_string":"new text"}}',
            '{"tool":"edit","args":{"path":"app.py","replacements":[{"old_string":"old1","new_string":"new1"},{"old_string":"old2","new_string":"new2"}]}}',
        ),
        description=(
            f"Create a new file with content, or edit an existing file with exact "
            f"replacements. Up to {MAX_REPLACEMENTS} replacements are validated "
            "together and written atomically."
        ),
        output_facts=("file_changed",),
        render_hint="edit",
        repair_hint="edit",
    ),
    ToolDefinition(
        "run",
        "run",
        permission="project_verify",
        examples=(
            '{"tool":"run","args":{"command":"python -m unittest","path":"."}}',
        ),
        description="Run an allowed test, build, lint, or check command.",
        output_facts=("command_verified",),
        render_hint="run",
        repair_hint="run",
    ),
    ToolDefinition(
        "shell",
        "shell",
        permission="user_approved_shell",
        examples=(
            '{"tool":"shell","args":{"command":"git status --short","path":"."}}',
        ),
        description="Ask the user to approve a necessary non-allowlisted command.",
        render_hint="shell",
        repair_hint="shell",
    ),
    ToolDefinition(
        "done",
        None,
        permission="control",
        examples=('{"tool":"done","args":{"summary":"direct final response to the user"}}',),
        description=(
            "Finish the task. summary is the complete user-facing response. "
            "For discussion, explanation, or read-only analysis, answer directly "
            "instead of merely saying that the topic was discussed."
        ),
        render_hint="done",
        repair_hint="done",
    ),
)


def _tool_definition_index() -> dict[str, ToolDefinition]:
    index: dict[str, ToolDefinition] = {}
    for definition in TOOL_DEFINITIONS:
        for name in (definition.name, *definition.aliases):
            if name in index:
                raise RuntimeError(f"duplicate tool contract name: {name}")
            index[name] = definition
    return index


TOOL_DEFINITION_BY_NAME = _tool_definition_index()
RUNTIME_TOOL_DEFINITION_BY_NAME = {
    definition.runtime_name: definition
    for definition in TOOL_DEFINITIONS
    if definition.runtime_name is not None
}
RESULT_TOOL_NAMES = {
    definition.runtime_name: definition.name
    for definition in TOOL_DEFINITIONS
    if definition.runtime_name is not None and definition.examples
}
SUPPORTED_RUNTIME_TOOL_NAMES = frozenset(RUNTIME_TOOL_DEFINITION_BY_NAME)
INFORMATION_RUNTIME_TOOL_NAMES = frozenset(
    name
    for name, definition in RUNTIME_TOOL_DEFINITION_BY_NAME.items()
    if definition.read_only or name in {"run", "shell"}
)


def definitions_for_permissions(permissions: tuple[str, ...] | list[str] | set[str]) -> tuple[ToolDefinition, ...]:
    allowed = set(permissions)
    return tuple(
        definition
        for definition in TOOL_DEFINITIONS
        if definition.permission in allowed
    )


def definitions_for_tool_names(names: tuple[str, ...] | list[str] | set[str]) -> tuple[ToolDefinition, ...]:
    requested = {str(name).lower().strip() for name in names}
    definitions: list[ToolDefinition] = []
    seen: set[str] = set()
    for definition in TOOL_DEFINITIONS:
        if definition.name in requested or any(alias in requested for alias in definition.aliases):
            if definition.name not in seen:
                definitions.append(definition)
                seen.add(definition.name)
    return tuple(definitions)


def render_tool_contract(definitions: tuple[ToolDefinition, ...] | None = None) -> str:
    definitions_to_render = TOOL_DEFINITIONS if definitions is None else definitions
    chunks: list[str] = []
    for definition in definitions_to_render:
        if not definition.examples:
            continue
        examples = "\n".join(f"  {example}" for example in definition.examples)
        chunks.append(f"{examples}\n    {definition.description}")
    return "\n\n".join(chunks)


def model_tool_contract_hash(definitions: tuple[ToolDefinition, ...] | None = None) -> str:
    """Hash the coding tool contract text currently visible to the model."""

    payload = {
        "kind": "coding_tool_contract",
        "contract": render_tool_contract(definitions),
    }
    data = json.dumps(payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
    return "sha256:" + hashlib.sha256(data.encode("utf-8")).hexdigest()


def public_example(
    tool_name: str,
    definitions: tuple[ToolDefinition, ...] | None = None,
) -> str:
    if definitions is None:
        definition = TOOL_DEFINITION_BY_NAME.get(tool_name)
    else:
        definition = next(
            (
                item
                for item in definitions
                if tool_name == item.name or tool_name in item.aliases
            ),
            None,
        )
    if definition is None or not definition.examples:
        return ""
    return definition.examples[0]


def runtime_definition(tool_name: str) -> ToolDefinition | None:
    return RUNTIME_TOOL_DEFINITION_BY_NAME.get(tool_name)


def render_tool_activity(call: ToolCall) -> str:
    definition = runtime_definition(call.name)
    hint = definition.render_hint if definition is not None else ""
    path = _call_arg(call, "path", ".")
    if hint == "read":
        return f"Reading {path}"
    if hint == "list":
        return f"Listing {path}"
    if hint == "search":
        query = _clip_activity(_call_arg(call, "query"))
        return f"Searching {path} for {query}" if query else f"Searching {path}"
    if hint == "references":
        symbol = _clip_activity(_call_arg(call, "symbol"))
        return f"Finding references for {symbol}" if symbol else "Finding references"
    if hint == "edit":
        return f"Writing {path}" if "content" in call.args else f"Editing {path}"
    if hint == "run":
        command = _clip_activity(_call_arg(call, "command"))
        return f"Running {command}" if command else "Running command"
    if hint == "shell":
        command = _clip_activity(_call_arg(call, "command"))
        return f"Requesting shell approval for {command}" if command else "Requesting shell approval"
    return f"Using {call.name}"


def _call_arg(call: ToolCall, name: str, default: str = "") -> str:
    value = call.args.get(name, default)
    if value is None:
        return default
    return str(value)


def _clip_activity(value: object, limit: int = 80) -> str:
    text = " ".join(str(value or "").split())
    if len(text) <= limit:
        return text
    return text[: limit - 3].rstrip() + "..."
