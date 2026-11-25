"""Apply the task-specific input bindings captured for a single model turn."""
from __future__ import annotations

from typing import TYPE_CHECKING

from codey.operations.browser.browser_upload import lower_upload_action
from codey.operations.browser.turn_surface import direct_browser_call, lower_browser_call
from codey.operations.kernel_tool_args import _canonical_name, _invalid_plan
from codey.operations.project.edit_base import (
    bind_symbol_read,
    bound_symbol_retry,
    lower_compact_symbol_edit,
    raw_symbol_content,
)
from codey.protocols.text_turn import DecodedTextTurn, TextAnswer
from codey.providers.base import AssistantTurn, TurnFinish
from codey.runtime.core.models import ToolPlan
from codey.toolchain.tool_spec import validate_args_with_spec
from codey.types.json_value import ToolArgs, canonical_json_object

if TYPE_CHECKING:
    from codey.operations.kernel.kernel_protocol import TurnSnapshot


def lower_bound_inputs(
    items: list[tuple[str, ToolArgs, str]], snapshot: TurnSnapshot,
) -> list[tuple[str, ToolArgs, str]] | ToolPlan:
    from codey.codemode.navigation import TASK_SOURCE_TOOLS

    binding = snapshot.input_binding
    models = {spec.name: spec for spec in snapshot.model_specs}
    checks = snapshot.policy.required_checks if snapshot.policy else ()
    lowered: list[tuple[str, ToolArgs, str]] = []
    for raw, args, call_id in items:
        name = _canonical_name(raw)
        if name == "save_finding" and name in models and snapshot.research_scope is not None:
            from codey.research.finding_choices import resolve_finding_passages

            args = resolve_finding_passages(args, snapshot.research_scope, models[name])
        if name in {"run", "edit"} and name in models:
            from codey.codemode.verification import resolve_verification

            try:
                args = (resolve_verification(args, {"properties": canonical_json_object(
                            dict(next(spec.parameters for spec in snapshot.frozen_specs if spec.name == "run")))})
                        if name == "run" else bind_symbol_read(
                            lower_compact_symbol_edit(args, models[name]), dict(snapshot.symbol_read_refs)))
            except ValueError as exc:
                return _invalid_plan(str(exc), tool=name)
        if name in TASK_SOURCE_TOOLS and name in models and snapshot.task_source_url is not None:
            from codey.codemode.navigation import resolve_task_source

            if error := validate_args_with_spec(models[name], args):
                return _invalid_plan(error, tool=name)
            lowered.append((raw, resolve_task_source(args, snapshot.task_source_url), call_id))
            continue
        direct = name in models and direct_browser_call(name, args, snapshot)
        if not direct and (binding is None or name != binding.model_spec.name or name not in models):
            lowered.append((raw, args, call_id))
            continue
        if error := validate_args_with_spec(models[name], args):
            return _invalid_plan(error, tool=name)
        try:
            if direct:
                if len(items) != 1:
                    return _invalid_plan("Submit one direct browser action per turn.", tool=name)
                lowered.append(("codemode", lower_browser_call(name, args, snapshot), call_id))
                continue
            assert binding is not None
            if binding.kind == "upload":
                converted = lower_upload_action(args, checks)
            elif binding.kind == "declared_fields":
                from codey.operations.project.declared_field_input import lower

                converted = lower(args, next(spec for spec in snapshot.frozen_specs if spec.name == "edit"))
                lowered.append(("codemode", converted, call_id))
                continue
            else:
                if snapshot.browser_copy is None:
                    raise ValueError("browser copy input has no captured observation")
                converted = snapshot.browser_copy.lower(args)
            lowered.append((raw, converted, call_id))
        except (KeyError, TypeError, ValueError) as exc:
            return _invalid_plan(str(exc), tool=name)
    return lowered


def bound_symbol_source(reply: DecodedTextTurn | AssistantTurn, snapshot: TurnSnapshot) -> str:
    if bound_symbol_retry(snapshot) is None:
        return ""
    if (isinstance(reply, AssistantTurn) and reply.raw.get("text_protocol") is True
            and reply.finish is TurnFinish.COMPLETE and not reply.tool_calls):
        return raw_symbol_content(reply.text)
    return raw_symbol_content(reply.text) if isinstance(reply, TextAnswer) else ""
