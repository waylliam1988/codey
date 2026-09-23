from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, Protocol


@dataclass(frozen=True)
class ProviderToolCall:
    id: str
    name: str
    arguments: dict[str, Any]


@dataclass(frozen=True)
class AssistantTurn:
    text: str = ""
    tool_calls: tuple[ProviderToolCall, ...] = ()
    raw: Mapping[str, object] = field(default_factory=dict)


class ChatProvider(Protocol):
    name: str

    @property
    def location(self) -> str:
        """Return a human-readable provider location or page URL."""

    def new_chat(self, timeout: float | None = None) -> None:
        """Start a fresh remote conversation."""

    def send(self, text: str, timeout: float | None = None) -> str:
        """Send one message and return the completed assistant response."""

    def close(self) -> None:
        """Release the local provider connection."""


class StructuredChatProvider(Protocol):
    name: str

    def new_chat(self, timeout: float | None = None) -> None: ...

    def send_turn(
        self,
        prompt: str,
        tools: list[dict[str, object]] | None = None,
        timeout: float | None = None,
    ) -> AssistantTurn: ...

    def send_tool_results(
        self,
        results: list[dict[str, object]],
        tools: list[dict[str, object]] | None = None,
        timeout: float | None = None,
    ) -> AssistantTurn: ...

    def close(self) -> None: ...
