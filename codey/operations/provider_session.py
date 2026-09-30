"""Per-exchange conversation accounting for either provider protocol."""

from __future__ import annotations

import contextlib
import json
import time
from typing import Any


def reply_text_for_accounting(reply: Any) -> str:
    if isinstance(reply, str):
        return reply
    return json.dumps({
        "text": getattr(reply, "text", ""),
        "tool_calls": [
            {"name": call.name, "args": call.arguments, "call_id": call.id}
            for call in (getattr(reply, "tool_calls", ()) or ())
        ],
    }, ensure_ascii=False, default=str)


class ProviderAdapter:
    def __init__(self, provider: Any) -> None:
        self.provider = provider

    def __getattr__(self, name: str) -> Any:
        value = getattr(self.provider, name)
        if name in {"send_turn", "send_tool_results"} and callable(value):
            return lambda *args, **kwargs: self._send(name, *args, **kwargs)
        return value

    def send(self, *args: Any, **kwargs: Any) -> Any:
        return self._send("send", *args, **kwargs)

    def normalize_reply(self, reply: Any) -> Any:
        from codey.operations.kernel_transport import normalize_provider_reply

        return normalize_provider_reply(self.provider, reply)

    def _send(self, name: str, *args: Any, **kwargs: Any) -> Any:
        return getattr(self.provider, name)(*args, **kwargs)


class ConversationProvider(ProviderAdapter):
    def __init__(self, provider: Any, conversation: Any, *,
                 fresh_window: tuple[str, str, str] | None = None) -> None:
        super().__init__(provider)
        self.conversation = conversation
        self.fresh_window = fresh_window

    def new_chat(self) -> Any:
        result = self.provider.new_chat()
        if self.fresh_window is not None:
            self.conversation.begin_window(*self.fresh_window)
        return result

    def _send(self, name: str, *args: Any, **kwargs: Any) -> Any:
        reply = getattr(self.provider, name)(*args, **kwargs)
        with contextlib.suppress(Exception):
            prompt = args[0] if args else ""
            if not isinstance(prompt, str):
                prompt = json.dumps(prompt, ensure_ascii=False, default=str)
            self.conversation.record_exchange(prompt, reply_text_for_accounting(reply))
        return reply


class DeadlineProvider(ProviderAdapter):
    """Propagate one episode's remaining budget to both provider protocols."""

    def __init__(self, provider: Any, deadline: float) -> None:
        super().__init__(provider)
        self.deadline = deadline

    def _send(self, name: str, *args: Any, **kwargs: Any) -> Any:
        remaining = self.deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("provider episode deadline exceeded")
        requested = kwargs.get("timeout")
        kwargs["timeout"] = min(remaining, requested) if requested is not None else remaining
        return getattr(self.provider, name)(*args, **kwargs)


class ObservedProvider(ProviderAdapter):
    """Observe an actual send attempt after durable intent creation."""

    def __init__(self, provider: Any, before_send: Any) -> None:
        super().__init__(provider)
        self.before_send = before_send

    def _send(self, name: str, *args: Any, **kwargs: Any) -> Any:
        self.before_send(name, args, kwargs)
        return super()._send(name, *args, **kwargs)
