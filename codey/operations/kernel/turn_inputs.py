"""Apply domain input conversions before canonical execution validation."""
from __future__ import annotations

from typing import TYPE_CHECKING

from codey.operations.browser.turn_surface import direct_browser_call, lower_browser_binding, lower_browser_call
from codey.operations.kernel_tool_args import _canonical_name, _invalid_plan
from codey.operations.project.model_inputs import lower_declared_fields, lower_project_input
from codey.operations.research.model_inputs import lower_research_input
from codey.runtime.core.models import ToolPlan
from codey.toolchain.tool_spec import validate_args_with_spec
from codey.types.json_value import ToolArgs

if TYPE_CHECKING:
    from codey.operations.kernel.turn_contract import TurnSnapshot


def lower_bound_inputs(
    items: list[tuple[str, ToolArgs, str]], snapshot: TurnSnapshot,
) -> list[tuple[str, ToolArgs, str]] | ToolPlan:
    binding = snapshot.input_binding
    models = {spec.name: spec for spec in snapshot.model_specs}
    lowered: list[tuple[str, ToolArgs, str]] = []
    for raw, args, call_id in items:
        name = _canonical_name(raw)
        model = models.get(name)
        if model is None:
            lowered.append((raw, args, call_id))
            continue
        try:
            if name in {"run", "edit"}:
                args = lower_project_input(args, model, snapshot.frozen_specs, snapshot.project.symbol_reads)
            else:
                args, source_selected = lower_research_input(args, model, snapshot.research.scope, snapshot.browser.source_url)
                if source_selected:
                    lowered.append((raw, args, call_id))
                    continue
            direct = direct_browser_call(name, args, snapshot)
            bound = binding is not None and name == binding.model_spec.name
            if direct or bound:
                if error := validate_args_with_spec(model, args):
                    return _invalid_plan(error, tool=name)
                if direct:
                    if len(items) != 1:
                        return _invalid_plan("Submit one direct browser action per turn.", tool=name)
                    lowered.append(("codemode", lower_browser_call(name, args, snapshot), call_id))
                    continue
                assert binding is not None
                if binding.kind == "declared_fields":
                    lowered.append(("codemode", lower_declared_fields(args, snapshot.frozen_specs), call_id))
                    continue
                args = lower_browser_binding(binding.kind, args, snapshot)
            lowered.append((raw, args, call_id))
        except (KeyError, TypeError, ValueError) as exc:
            return _invalid_plan(str(exc), tool=name)
    return lowered
