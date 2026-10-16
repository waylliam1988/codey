"""Bounded UI snapshots of native text; never reasoning or function arguments."""
from __future__ import annotations

import logging
import time
from collections.abc import Callable
from typing import Any
from uuid import uuid4

logger = logging.getLogger(__name__)


class DisplayStream:
    def __init__(self, emit: Callable[[dict[str, Any]], None]) -> None:
        self.emit = emit
        self.identity = uuid4().hex
        self.text = ""
        self.sequence = 0
        self.last_emit = 0.0

    def observe(self, event: dict[str, Any]) -> None:
        fragments: list[str] = []
        if event.get("type") == "response.output_text.delta":
            value = event.get("delta")
            if isinstance(value, str):
                fragments.append(value)
        for choice in event.get("choices", []):
            if choice.get("index", 0) != 0:
                continue
            value = (choice.get("delta") or {}).get("content")
            if isinstance(value, str):
                fragments.append(value)
        if not fragments:
            return
        self.text = (self.text + "".join(fragments))[:64_000]
        if not self.sequence or time.monotonic() - self.last_emit >= 0.05:
            self._publish("partial")

    def finish(self, text: str | None = None) -> None:
        if not self.text:
            return
        if text is not None:
            self.text = text[:64_000]
        self._publish("complete" if text is not None else "interrupted")

    def _publish(self, state: str) -> None:
        self.sequence += 1
        self.last_emit = time.monotonic()
        try:
            self.emit({"type": "reply_stream", "stream_id": self.identity,
                       "sequence": self.sequence, "state": state, "text": self.text})
        except Exception:
            # A failed display sink must not retry a generated request.
            logger.exception("reply display snapshot could not be published")
