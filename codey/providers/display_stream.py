"""Bounded provisional text/ordinary done.summary; never private metadata."""
from __future__ import annotations

import logging
import time
from collections.abc import Callable
from uuid import uuid4

from codey.ghost.payload_types import mapping_fields
from codey.providers.summary_stream import summary_prefix

logger = logging.getLogger(__name__)


class DisplayStream:
    def __init__(self, emit: Callable[[dict[str, object]], None]) -> None:
        self.emit = emit
        self.identity = uuid4().hex
        self.text = ""
        self.sequence = 0
        self.last_emit = 0.0
        self.call_names: dict[int, str] = {}
        self.call_args: dict[int, str] = {}
        self.summary_text = ""

    def observe(self, event: dict[str, object]) -> None:
        fragments: list[str] = []
        if event.get("type") == "response.output_text.delta":
            value = event.get("delta")
            if isinstance(value, str):
                fragments.append(value)
        choices = event.get("choices", [])
        items: list[object] = []
        if isinstance(choices, list):
            items = choices
        for choice in items:
            if not mapping_fields(choice) or choice.get("index", 0) != 0:
                continue
            delta = choice.get("delta")
            if not mapping_fields(delta):
                continue
            value = delta.get("content")
            if isinstance(value, str):
                fragments.append(value)
            calls = delta.get("tool_calls", [])
            call_items: list[object] = []
            if isinstance(calls, list):
                call_items = calls
            for call in call_items:
                if not mapping_fields(call) or type(call.get("index")) is not int:
                    continue
                slot = call["index"]
                if not isinstance(slot, int):
                    continue
                function = call.get("function")
                if mapping_fields(function):
                    self._arguments(slot, function.get("name"), function.get("arguments"))
        slot = event.get("output_index")
        if type(slot) is int and isinstance(slot, int):
            item = event.get("item")
            if event.get("type") == "response.output_item.added" and mapping_fields(item):
                self._arguments(slot, item.get("name"), item.get("arguments"))
            elif event.get("type") == "response.function_call_arguments.delta":
                self._arguments(slot, None, event.get("delta"))
        if not fragments:
            if not self.summary_text or self.text == self.summary_text:
                return
            self.text = self.summary_text
        else:
            self.text = (self.text + "".join(fragments))[:64_000]
        if not self.sequence or time.monotonic() - self.last_emit >= 0.05:
            self._publish("partial")

    def finish(self, text: str | None = None) -> None:
        if not self.text:
            return
        if text is not None:
            self.text = (text or self.summary_text)[:64_000]
        self._publish("complete" if text is not None else "interrupted")

    def _arguments(self, slot: int, name: object, arguments: object) -> None:
        if isinstance(name, str):
            self.call_names[slot] = (self.call_names.get(slot, "") + name)[:120]
        if isinstance(arguments, str):
            self.call_args[slot] = (self.call_args.get(slot, "") + arguments)[:128_000]
        if len(self.call_names) == 1 and self.call_names.get(slot) == "done":
            self.summary_text = summary_prefix(self.call_args.get(slot, ""))[:64_000]

    def _publish(self, state: str) -> None:
        self.sequence += 1
        self.last_emit = time.monotonic()
        try:
            self.emit({"type": "reply_stream", "stream_id": self.identity,
                       "sequence": self.sequence, "state": state, "text": self.text})
        except Exception:
            # A failed display sink must not retry a generated request.
            logger.exception("reply display snapshot could not be published")
