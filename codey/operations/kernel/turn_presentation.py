"""Project the model-facing declarations for task input bindings."""
from __future__ import annotations

from dataclasses import replace
from typing import TYPE_CHECKING

from codey.codemode.catalog import render_declarations
from codey.operations.browser import browser_followups
from codey.operations.browser.browser_references import reference_spec
from codey.operations.browser.catalog import snapshot_catalog
from codey.toolchain.tool_prompt import json_contract_text

if TYPE_CHECKING:
    from codey.operations.kernel.turn_contract import TurnSnapshot
    from codey.operations.kernel_contracts import ToolExecutor
    from codey.toolchain.tool_spec import ToolSpec


def model_tool_specs(snapshot: TurnSnapshot) -> tuple[ToolSpec, ...]:
    from codey.codemode.verification import verification_model_spec

    binding = snapshot.input_binding
    specs = tuple(binding.model_spec if binding is not None and spec.name == binding.model_spec.name else spec
                  for spec in snapshot.frozen_specs if spec.name in snapshot.tool_names)
    specs = tuple(reference_spec(spec, snapshot.browser_references) for spec in specs)
    specs = tuple(verification_model_spec(spec) if spec.name == "run" else spec for spec in specs)
    if snapshot.task_source_url is not None:
        from codey.codemode.navigation import TASK_SOURCE_TOOLS, task_source_spec

        specs = tuple(task_source_spec(spec) if spec.name in TASK_SOURCE_TOOLS else spec for spec in specs)
    if not snapshot.browser_followups:
        return specs
    if snapshot.task_navigation_url is not None:
        from codey.codemode.navigation import task_navigation_spec

        specs = tuple(task_navigation_spec(spec) if spec.name == "browser_navigate" else spec for spec in specs)
    plans = browser_followups.click_plans(snapshot.policy.required_checks if snapshot.policy else (),
                                          set(snapshot.nested_tool_names))
    purposes = tuple(purpose for purpose in browser_followups.PURPOSES if purpose in plans)
    return tuple(browser_followups.model_spec(spec, snapshot.browser_fill_text, click_purposes=purposes,
                 navigation_observes=snapshot.navigation_observes,
                 navigation_verifies=snapshot.navigation_verifies) for spec in specs)


def render_snapshot_contract(snapshot: TurnSnapshot) -> str:
    text = json_contract_text(snapshot.model_specs)
    if snapshot.browser_copy is not None and snapshot.browser_copy.phase == "question":
        text += "\nAsk which webpage source to use. The observation has no unique supported data format."
    return text


def refresh_snapshot_contract(snapshot: TurnSnapshot, *, compact_catalog: bool = False,
                              include_json_edit: bool = False) -> TurnSnapshot:
    if snapshot.input_binding is not None and snapshot.input_binding.model_spec.name not in snapshot.tool_names:
        snapshot = replace(snapshot, input_binding=None)
    order = {name: index for index, name in enumerate(snapshot.inventory_names)}

    def spec_order(spec: ToolSpec) -> int:
        return order.get(spec.name, len(order))

    def executor_order(item: tuple[str, ToolExecutor | None]) -> int:
        return order.get(item[0], len(order))

    snapshot = replace(snapshot,
        frozen_specs=tuple(sorted(snapshot.frozen_specs, key=spec_order)),
        custom_executors=tuple(sorted(snapshot.custom_executors, key=executor_order)))
    if "codemode" in snapshot.tool_names and (
            snapshot.input_binding is None or snapshot.input_binding.model_spec.name != "codemode"):
        specs = model_tool_specs(snapshot)
        declarations = render_declarations(snapshot_catalog(snapshot),
            outer_descriptions={spec.name: spec.description for spec in specs}, outer_specs=specs,
            detail_budget_bytes=2_400 if compact_catalog else None, detail_names=("edit",),
            include_json_edit=include_json_edit)
        snapshot = replace(snapshot, frozen_specs=tuple(
            replace(spec, declarations=declarations) if spec.name == "codemode" else spec for spec in snapshot.frozen_specs))
    snapshot = replace(snapshot, model_specs=model_tool_specs(snapshot))
    return replace(snapshot, contract_text=render_snapshot_contract(snapshot))
