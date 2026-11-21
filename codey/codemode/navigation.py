"""One task-source selector shared by direct presentation and CodeMode."""
from __future__ import annotations

from dataclasses import replace

from codey.knowledge.model import canonical_json
from codey.toolchain.tool_spec import ToolSpec, freeze_spec_parameters
from codey.types.json_value import JsonObject, canonical_json_object

TASK_TARGET = "task"
TASK_SOURCE_TOOLS = frozenset({"open_url", "source_search"})


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


def task_source_schema(original: JsonObject) -> JsonObject:
    properties = canonical_json_object(original["properties"])
    required = original.get("required", [])
    if not isinstance(required, list):
        raise ValueError("Source arguments require a field list")
    selector = dict(task_navigation_parameters())["target"]
    target: JsonObject = {"anyOf": [selector, {"type": "object", "properties": {"url": properties["url"]},
        "required": ["url"], "additionalProperties": False}]}
    return {**original, "properties": {**{key: value for key, value in properties.items() if key != "url"}, "target": target},
            "required": ["target" if key == "url" else key for key in required]}


def task_source_spec(spec: ToolSpec) -> ToolSpec:
    schema = task_source_schema({"type": "object", "properties": canonical_json_object(dict(spec.parameters)),
                               "required": list(spec.required), "additionalProperties": False})
    required = schema["required"]
    assert isinstance(required, list)
    return freeze_spec_parameters(replace(spec, parameters=tuple(canonical_json_object(schema["properties"]).items()),
        required=tuple(key for key in required if isinstance(key, str)), json_examples=(),
        description=spec.description + " Select target=task for the original task source; the application supplies "
        "its exact URL. For another authorized source select target={url: ...}; "
        f"tools.{spec.name}({{url: ...}}) in CodeMode also remains available."))


def resolve_task_source(args: JsonObject, url: str | None) -> JsonObject:
    if "target" not in args:
        return args
    target = args["target"]
    if "url" in args:
        raise ValueError('Use either target="task" or an explicit URL, never both')
    if isinstance(target, dict) and set(target) == {"url"} and isinstance(target["url"], str):
        return {**{key: value for key, value in args.items() if key != "target"}, "url": target["url"]}
    if target != TASK_TARGET:
        raise ValueError('Select target="task" or target={url: "..."}')
    if url is None:
        raise ValueError("This turn has no bound task URL")
    return {**{key: value for key, value in args.items() if key != "target"}, "url": url}
