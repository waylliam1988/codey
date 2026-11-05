"""Token usage only; unexpected nonzero user cost stops further admission."""
from __future__ import annotations

from decimal import Decimal, InvalidOperation
from typing import Any

from codey.providers.cline.codec import unwrap
from codey.providers.cline.runtime import Runtime
from codey.providers.local_usage import parse_chat_usage
from codey.providers.token_accounting import ReportedUsage


def parse_usage(runtime: Runtime, event: dict[str, Any]) -> ReportedUsage | None:
    event = unwrap(event)
    usage = event.get("usage")
    if isinstance(usage, dict) and usage.get("cost") is not None:
        try:
            amount = Decimal(str(usage["cost"]))
            unexpected = not amount.is_finite() or amount != 0
        except InvalidOperation:
            unexpected = True
        if unexpected:
            runtime.block_unexpected_cost()
    return parse_chat_usage(event)
