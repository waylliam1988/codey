"""Request counting boundary; factories select a method before generation."""
from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any, Protocol

from codey.providers.token_accounting import ReportedUsage, RequestContextCount
from codey.utils.text_budget import estimate_tokens

UsageParser = Callable[[dict[str, Any]], ReportedUsage | None]


def no_reported_usage(event: dict[str, Any]) -> ReportedUsage | None:
    return None


class UsageCollector:
    """One request's latest normalized snapshot; never sums cumulative frames."""

    def __init__(self, parser: UsageParser) -> None:
        self.parser = parser
        self.usage = ReportedUsage()
        self.status = "missing"

    def observe(self, event: dict[str, Any]) -> None:
        try:
            usage = self.parser(event)
        except (ValueError, TypeError, KeyError):
            self.usage, self.status = ReportedUsage(), "invalid"
            return
        if usage is not None:
            self.usage, self.status = usage, "reported"


class RequestCounter(Protocol):
    def __call__(self, payload: dict[str, Any], *, deadline: float) -> RequestContextCount: ...


def estimate_request(payload: dict[str, Any], *, deadline: float) -> RequestContextCount:
    """Declared approximate mode; never used after a tokenizer failure."""
    from codey.task.attachments import image_estimate_view

    view, images = image_estimate_view(payload)
    return RequestContextCount(estimate_tokens(json.dumps(view, ensure_ascii=False)) + images, "estimated")
