"""Resolve model-visible research references to frozen source identities."""
from __future__ import annotations

from typing import TYPE_CHECKING

from codey.codemode.navigation import TASK_SOURCE_TOOLS, resolve_task_source
from codey.research.finding_choices import resolve_finding_passages
from codey.toolchain.tool_spec import validate_args_with_spec
from codey.types.json_value import ToolArgs

if TYPE_CHECKING:
    from codey.research.answer_contract import AnswerScope
    from codey.toolchain.spec_model import ToolSpec


def lower_research_input(
    args: ToolArgs, model: ToolSpec, scope: AnswerScope | None, source_url: str | None,
) -> tuple[ToolArgs, bool]:
    if model.name == "save_finding" and scope is not None:
        return resolve_finding_passages(args, scope, model), False
    if model.name in TASK_SOURCE_TOOLS and source_url is not None:
        if error := validate_args_with_spec(model, args):
            raise ValueError(error)
        return resolve_task_source(args, source_url), True
    return args, False
