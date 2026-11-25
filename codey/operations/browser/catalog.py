"""Present the browser's frozen choices to CodeMode's tool catalog."""
from __future__ import annotations

from typing import TYPE_CHECKING

from codey.codemode.catalog import build_codemode_catalog
from codey.operations.browser.browser_followups import PURPOSES, add_link_observation, click_plans, model_spec
from codey.operations.browser.browser_references import reference_spec
from codey.types.json_value import JsonObject

if TYPE_CHECKING:
    from codey.codemode.types import ToolCatalogSnapshot
    from codey.operations.kernel.turn_contract import TurnSnapshot
    from codey.toolchain.spec_model import ToolSpec


def browser_catalog_specs(
    specs: tuple[ToolSpec, ...], checks: tuple[str, ...], *, observe_links: bool,
) -> tuple[tuple[ToolSpec, ...], JsonObject]:
    plans = click_plans(checks, {spec.name for spec in specs})
    control_steps = plans.get("controls")
    if isinstance(control_steps, list) and any(spec.name == "browser_read" and not spec.required for spec in specs):
        plans["controls"] = [*control_steps, {"name": "browser_read", "args": JsonObject()}]
    add_link_observation(plans, checks, enabled=observe_links)
    purposes = tuple(purpose for purpose in PURPOSES if purpose in plans)
    return tuple(model_spec(spec, None, click_purposes=purposes) if spec.name == "browser_click" else spec
                 for spec in specs), plans


def snapshot_catalog(
    snapshot: TurnSnapshot, *, allow_writes: bool = True, observed_choices: bool = True,
) -> ToolCatalogSnapshot:
    """Derive model choices or replay capabilities from the same frozen specs."""
    names = set(snapshot.nested_tool_names)
    writable = allow_writes and snapshot.policy is not None and snapshot.policy.allows("project.write")
    specs = tuple(reference_spec(spec, snapshot.browser.references if observed_choices else None)
                  for spec in snapshot.frozen_specs if spec.name in names)
    plans = None
    if snapshot.browser.followups:
        specs, plans = browser_catalog_specs(specs, snapshot.policy.required_checks if snapshot.policy else (),
                                             observe_links=snapshot.research.scope is None)
    return build_codemode_catalog(
        specs,
        allow_project_edit=writable and "edit" in names,
        allow_project_run=allow_writes and snapshot.policy is not None and snapshot.policy.allows("project.verify")
            and "run" in names,
        task_navigation_url=snapshot.browser.navigation_url,
        task_source_url=snapshot.browser.source_url,
        task_source_selector=snapshot.browser.source_url is not None,
        click_followups=plans,
        browser_result_reads=snapshot.research.scope is not None,
        browser_flows=snapshot.browser.followups,
        semantic_edits=True,
    )
