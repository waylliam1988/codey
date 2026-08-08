"""Provider wrapper for Ghost signal extraction."""

from __future__ import annotations

from typing import Protocol

from codey.ghost.schema import GhostSignalParseResult, clip_signal_text
from codey.ghost.signal_codec import GhostSignalCodec


class SignalProvider(Protocol):
    def send(self, text: str, timeout: float | None = None) -> str:
        """Send one message and return the completed assistant response."""


class GhostSignalExtractor:
    """Run the Ghost signal JSON contract against a ChatProvider.

    The extractor is fail-open: provider or parsing failures produce no signals.
    It is intended for manual/shadow use in 0.3.x, not for blocking user-facing
    chat, coding, or Research turns.
    """

    def __init__(self, codec: GhostSignalCodec | None = None) -> None:
        self.codec = codec or GhostSignalCodec()

    def extract_from_reply(
        self,
        reply: str,
        *,
        user_text: str,
        provider_id: str = "",
    ) -> GhostSignalParseResult:
        try:
            return self.codec.parse(reply, user_text=user_text, provider_id=provider_id)
        except Exception as exc:
            return GhostSignalParseResult(
                diagnostics=(f"parse_error: {clip_signal_text(exc, 160)}",),
                ok=False,
                raw_text_chars=len(reply or ""),
                provider_id=provider_id,
            )

    def extract(
        self,
        *,
        provider: SignalProvider,
        user_text: str,
        assistant_text: str = "",
        context: str = "",
        provider_id: str = "",
        timeout: float | None = None,
    ) -> GhostSignalParseResult:
        try:
            prompt = self.codec.format_request(
                user_text=user_text,
                assistant_text=assistant_text,
                context=context,
            )
            reply = provider.send(prompt, timeout=timeout)
        except Exception as exc:
            return GhostSignalParseResult(
                diagnostics=(f"provider_error: {clip_signal_text(exc, 160)}",),
                ok=False,
                provider_id=provider_id,
            )
        return self.extract_from_reply(reply, user_text=user_text, provider_id=provider_id)
