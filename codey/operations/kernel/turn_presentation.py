"""Project the model-facing declarations for task input bindings."""
from __future__ import annotations

from typing import TYPE_CHECKING

from codey.operations.browser import browser_followups
from codey.operations.browser.browser_references import reference_spec

if TYPE_CHECKING:
    from codey.operations.kernel.kernel_protocol import TurnSnapshot
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
