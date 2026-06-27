from __future__ import annotations

from typing import Protocol

from codey.models import ToolPlan, ToolResult


class ProtocolCodec(Protocol):
    name: str

    def system_prompt(self) -> str:
        """Return the model-facing protocol instructions."""

    def parse(self, text: str) -> ToolPlan:
        """Parse an assistant reply into local actions plus a control signal."""

    def format_results(self, results: list[ToolResult]) -> str:
        """Format tool results for the next model turn."""

    def repair_prompt(self) -> str:
        """Return a short prompt asking the model to fix protocol formatting."""
