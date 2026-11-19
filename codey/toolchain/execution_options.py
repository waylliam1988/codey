"""Separate validated scheduling options from the original business call."""
from __future__ import annotations

from dataclasses import dataclass

from codey.types.json_value import ToolArgs


@dataclass(frozen=True)
class ExecutionOptions:
    background: bool = False


def split_execution_options(args: ToolArgs) -> tuple[ToolArgs, ExecutionOptions]:
    business = dict(args)
    background = business.pop("run_in_background", False)
    if not isinstance(background, bool):
        raise ValueError("run_in_background must be a boolean")
    return business, ExecutionOptions(background)
