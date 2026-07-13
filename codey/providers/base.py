from __future__ import annotations

from typing import Protocol


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
