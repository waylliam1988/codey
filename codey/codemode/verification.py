"""Choose the frozen project check without copying its command or directory."""
from __future__ import annotations

from dataclasses import replace

from codey.completion.verification_policy import VerificationPlan, VerificationRequirement
from codey.knowledge.model import canonical_json
from codey.toolchain.tool_spec import ToolSpec, freeze_spec_parameters
from codey.types.json_value import JsonObject, canonical_json_object, require_json_string

CHECK: JsonObject = {"check": "project"}
_BINDING = "x-codey-project-check"


def project_check_call(spec: ToolSpec) -> JsonObject | None:
    command = canonical_json_object(dict(spec.parameters)["command"])
    binding = command.get(_BINDING)
    return {"tool": "run", "args": {"command": CHECK}} if isinstance(binding, dict) and "command" in binding else None


def matches_selected_check(spec: ToolSpec, args: JsonObject) -> bool:
    """Compare an admitted concrete run with the original frozen check choices."""
    binding = canonical_json_object(dict(spec.parameters)["command"]).get(_BINDING)
    if not isinstance(binding, dict):
        return False
    choices = binding.get("choices")
    candidates = choices.values() if isinstance(choices, dict) else (binding,)
    return any(isinstance(candidate, dict) and candidate.get("command") == args.get("command")
               and candidate.get("path") == args.get("path", ".") for candidate in candidates)


def verification_spec(spec: ToolSpec, candidate: VerificationRequirement) -> ToolSpec:
    if isinstance(candidate, VerificationPlan):
        return verification_plan_spec(spec, candidate)
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



def verification_plan_spec(spec: ToolSpec, plan: VerificationPlan) -> ToolSpec:
    bindings: JsonObject = {
        f"project-{index}": {"command": obligation.candidate.command, "path": obligation.candidate.cwd or "."}
        for index, obligation in enumerate(plan.obligations, 1)
    }
    field = canonical_json_object(dict(spec.parameters)["command"])
    command: JsonObject = {
        "anyOf": [field, {"type": "object", "properties": {"check": {"type": "string", "enum": list(bindings)}},
                          "required": ["check"], "additionalProperties": False}],
        _BINDING: {"choices": bindings},
    }
    return freeze_spec_parameters(replace(spec, parameters=tuple(
        (key, command if key == "command" else value) for key, value in spec.parameters),
        json_examples=tuple(canonical_json({"tool": "run", "args": {"command": {"check": key}}}) for key in bindings),
        description=spec.description + " Run each required project check after all requested edits. "
        "Select its exact command and path using command={check: ID}. Checks: " + canonical_json(bindings)))

def resolve_verification(args: JsonObject, parameters: JsonObject) -> JsonObject:
    if not isinstance(args.get("command"), dict):
        return args
    properties = canonical_json_object(parameters["properties"])
    binding = canonical_json_object(properties["command"]).get(_BINDING)
    requested = canonical_json_object(args["command"])
    if not isinstance(binding, dict) or set(requested) != {"check"}:
        raise ValueError("Select the project check from this frozen request")
    choices = binding.get("choices")
    if isinstance(choices, dict) and isinstance(key := requested["check"], str):
        binding = choices.get(key)
    elif requested != CHECK:
        binding = None
    if not isinstance(binding, dict):
        raise ValueError("Select the project check from this frozen request")
    command = require_json_string(binding["command"], "project check command")
    path = require_json_string(binding["path"], "project check path")
    if "path" in args and args["path"] != path:
        raise ValueError("The supplied path conflicts with the selected project check")
    return {**args, "command": command, "path": path}
