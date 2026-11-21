"""Choose the frozen project check without copying its command or directory."""
from __future__ import annotations

from dataclasses import replace

from codey.completion.verification_policy import VerificationCandidate
from codey.knowledge.model import canonical_json
from codey.toolchain.tool_spec import ToolSpec, freeze_spec_parameters
from codey.types.json_value import JsonObject, canonical_json_object, require_json_string

CHECK: JsonObject = {"check": "project"}
_BINDING = "x-codey-project-check"


def project_check_call(spec: ToolSpec) -> JsonObject | None:
    command = canonical_json_object(dict(spec.parameters)["command"])
    return {"tool": "run", "args": {"command": CHECK}} if _BINDING in command else None


def verification_spec(spec: ToolSpec, candidate: VerificationCandidate) -> ToolSpec:
    command: JsonObject = {"anyOf": [canonical_json_object(dict(spec.parameters)["command"]),
        {"type": "object", "properties": {"check": {"type": "string", "enum": ["project"]}},
         "required": ["check"], "additionalProperties": False}],
        _BINDING: {"command": candidate.command, "path": candidate.cwd or "."}}
    return freeze_spec_parameters(replace(spec, parameters=tuple(
        (key, command if key == "command" else value) for key, value in spec.parameters),
        json_examples=(canonical_json({"tool": "run", "args": {"command": CHECK}}), *spec.json_examples),
        description=spec.description + " To run the selected project check without retyping it, supply command="
        + canonical_json(CHECK) + "; the application supplies its exact command and working directory: "
        + canonical_json(command[_BINDING]) + ". Other allowed verification commands remain available."))


def resolve_verification(args: JsonObject, parameters: JsonObject) -> JsonObject:
    if not isinstance(args.get("command"), dict):
        return args
    properties = canonical_json_object(parameters["properties"])
    binding = canonical_json_object(properties["command"]).get(_BINDING)
    if args["command"] != CHECK or not isinstance(binding, dict):
        raise ValueError("Select the project check from this frozen request")
    command = require_json_string(binding["command"], "project check command")
    path = require_json_string(binding["path"], "project check path")
    if "path" in args and args["path"] != path:
        raise ValueError("The supplied path conflicts with the selected project check")
    return {**args, "command": command, "path": path}
