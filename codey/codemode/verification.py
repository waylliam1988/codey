"""Choose the frozen project check without copying its command or directory."""
from __future__ import annotations

import re
from dataclasses import replace

from codey.completion.verification_policy import VerificationPlan, VerificationRequirement
from codey.knowledge.model import canonical_json
from codey.toolchain.tool_spec import ToolSpec, freeze_spec_parameters
from codey.types.json_value import JsonObject, canonical_json_object, require_json_string

CHECK: JsonObject = {"check": "project"}
_BINDING = "x-codey-project-check"
_SELECTED_ONLY = "x-codey-selected-check-only"


def project_check_call(spec: ToolSpec) -> JsonObject | None:
    if _selected_only(spec):
        return {"tool": "run", "args": CHECK.copy()}
    return next((call for args, call in project_check_choices(spec) if call["args"] == {"command": CHECK}), None)


def project_check_choices(spec: ToolSpec) -> tuple[tuple[JsonObject, JsonObject], ...]:
    field = dict(spec.parameters).get("command")
    if field is None:
        return ()
    binding = canonical_json_object(field).get(_BINDING)
    if not isinstance(binding, dict):
        return ()
    raw = binding.get("choices")
    choices = raw.items() if isinstance(raw, dict) else (("project", binding),)
    return tuple((candidate, {"tool": "run", "args": {"command": {"check": key}}})
                 for key, candidate in choices if isinstance(candidate, dict))


def matches_selected_check(spec: ToolSpec, args: JsonObject) -> bool:
    """Compare an admitted concrete run with the original frozen check choices."""
    return any(candidate.get("command") == args.get("command") and candidate.get("path") == args.get("path", ".")
               for candidate, _ in project_check_choices(spec))


def verification_spec(spec: ToolSpec, candidate: VerificationRequirement, *,
                      background: bool | None = None) -> ToolSpec:
    if isinstance(candidate, VerificationPlan):
        return verification_plan_spec(spec, candidate)
    check: JsonObject = {"command": candidate.command, "path": candidate.cwd or "."}
    if background is not None:
        check["run_in_background"] = background
    command = _check_parameter(spec, {"project": check})
    selected = freeze_spec_parameters(replace(spec, parameters=tuple(
        (key, command if key == "command" else value) for key, value in spec.parameters),
        json_examples=(canonical_json({"tool": "run", "args": {"command": CHECK}}), *spec.json_examples),
        description=spec.description + " To run the selected project check without retyping it, supply command="
        + canonical_json(CHECK) + "; the application supplies its exact command and working directory: "
        + canonical_json(command[_BINDING]) + "."))
    return restrict_verification(selected) if background is not None else selected

def verification_plan_spec(spec: ToolSpec, plan: VerificationPlan) -> ToolSpec:
    bindings: JsonObject = {
        f"project-{index}": {"command": obligation.candidate.command, "path": obligation.candidate.cwd or "."}
        for index, obligation in enumerate(plan.obligations, 1)
    }
    command = _check_parameter(spec, bindings)
    return freeze_spec_parameters(replace(spec, parameters=tuple(
        (key, command if key == "command" else value) for key, value in spec.parameters),
        json_examples=tuple(canonical_json({"tool": "run", "args": {"command": {"check": key}}}) for key in bindings),
        description=spec.description + " Run each required project check after all requested edits. "
        "Select its exact command and path using command={check: ID}. Checks: " + canonical_json(bindings)))


def _check_parameter(spec: ToolSpec, bindings: JsonObject) -> JsonObject:
    selector: JsonObject = {"type": "object", "properties": {"check": {"type": "string", "enum": list(bindings)}},
                            "required": ["check"], "additionalProperties": False}
    field = canonical_json_object(dict(spec.parameters)["command"])
    return {"anyOf": [field, selector], _BINDING: {"choices": bindings}}


def _selected_only(spec: ToolSpec) -> bool:
    command = dict(spec.parameters).get("command")
    return command is not None and canonical_json_object(command).get(_SELECTED_ONLY) is True


def restrict_verification(spec: ToolSpec) -> ToolSpec:
    if project_check_call(spec) is None:
        return spec
    field = canonical_json_object(dict(spec.parameters)["command"])
    field[_SELECTED_ONLY] = True
    return freeze_spec_parameters(replace(spec, parameters=tuple(
        (name, field if name == "command" else value) for name, value in spec.parameters)))


def verification_model_spec(spec: ToolSpec) -> ToolSpec:
    if not _selected_only(spec):
        return spec
    return freeze_spec_parameters(replace(spec,
        parameters=(("check", {"type": "string", "enum": ["project"]}),), required=("check",),
        json_examples=(canonical_json(project_check_call(spec)),),
        description="Run the selected project check. The host supplies its exact command, directory and execution mode."))


def resolve_verification(args: JsonObject, parameters: JsonObject) -> JsonObject:
    raw = args.get("command")
    properties = parameters.get("properties")
    field = properties.get("command") if isinstance(properties, dict) else None
    binding = field.get(_BINDING) if isinstance(field, dict) else None
    selected_only = isinstance(field, dict) and field.get(_SELECTED_ONLY) is True
    if "check" in args:
        if not selected_only or args != CHECK:
            raise ValueError('Use the check offered for this request, such as {"check":"project"}.')
        args = {"command": CHECK}
        raw = args["command"]
    if isinstance(raw, str):
        if selected_only:
            choices = binding.get("choices") if isinstance(binding, dict) else None
            selected = choices.get("project") if isinstance(choices, dict) else None
            if (isinstance(selected, dict) and raw == selected.get("command") and
                    args.get("path", ".") == selected.get("path") and "run_in_background" not in selected):
                return args
            raise ValueError('Run the selected check using {"check":"project"}.')
        if re.match(r"""^\s*\{\s*['"]check['"]\s*:""", raw):
            raise ValueError('Select a check using a command object such as {"check":"project"}.')
        return args
    if not isinstance(raw, dict):
        return args
    requested = canonical_json_object(raw)
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
    lowered = {**args, "command": command, "path": path}
    if isinstance(background := binding.get("run_in_background"), bool):
        if args.get("run_in_background", background) is not background:
            raise ValueError("The execution mode conflicts with the selected project check.")
        lowered["run_in_background"] = background
    return lowered
