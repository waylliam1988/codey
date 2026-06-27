from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol


@dataclass
class Action:
    kind: str
    path: str | None
    body: str


@dataclass
class Control:
    kind: str
    body: str


class ProtocolCodec(Protocol):
    name: str

    def system_prompt(self) -> str:
        """Return the model-facing protocol instructions."""

    def parse(self, text: str) -> tuple[list[Action], Control | None]:
        """Parse an assistant reply into local actions plus a control signal."""

    def format_results(self, results: list[tuple[Action, str]]) -> str:
        """Format tool results for the next model turn."""

    def repair_prompt(self) -> str:
        """Return a short prompt asking the model to fix protocol formatting."""
