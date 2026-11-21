"""One task-source selector shared by direct presentation and CodeMode."""
from __future__ import annotations

from dataclasses import replace

from codey.knowledge.model import canonical_json
from codey.toolchain.tool_spec import ToolSpec, freeze_spec_parameters
from codey.types.json_value import JsonObject

TASK_TARGET = "task"


def task_navigation_parameters() -> tuple[tuple[str, JsonObject], ...]:
    return (("target", {"type": "string", "enum": [TASK_TARGET]}),)


def task_navigation_schema() -> JsonObject:
    parameters = task_navigation_parameters()
    return {"type": "object", "properties": {key: value for key, value in parameters},
            "required": [key for key, _ in parameters], "additionalProperties": False}


def task_navigation_spec(spec: ToolSpec) -> ToolSpec:
    parameters = task_navigation_parameters()
    return freeze_spec_parameters(replace(spec, parameters=parameters,
        required=tuple(key for key, _ in parameters),
        description="Open the original task source at the chosen time. Select target=task; "
            "the application supplies the exact URL from this frozen request. "
            "For another authorized page, use tools.browser_navigate({url: ...}) in CodeMode.",
        json_examples=(canonical_json({"tool": spec.name, "args": {"target": TASK_TARGET}}),)))


def resolve_task_navigation(args: JsonObject, url: str | None) -> JsonObject:
    if "target" not in args:
        return args
    if args != {"target": TASK_TARGET}:
        raise ValueError('Use either {target: "task"} or {url: "..."}')
    if url is None:
        raise ValueError("This turn has no bound task URL")
    return {"url": url}
