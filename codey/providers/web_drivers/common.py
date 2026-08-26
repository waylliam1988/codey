"""Shared plumbing for the site-specific web chat drivers.

Every driver repeats the same control-location, response-counting,
rate-limit-detection, and late-response-polling scaffolding around its own
selectors and completion heuristics. This module owns the scaffolding once;
drivers keep only what is genuinely site-specific.
"""

from __future__ import annotations

import re
import time
from typing import Any, Callable

from codey.runtime import cancellation
from codey.providers.controls import (
    CONTROL_MESSAGE_BOX,
    locate_control,
    locate_response,
    response_count as _controls_response_count,
)
from codey.providers.profiles import ProviderProfile


def message_box(
    provider_id: str,
    profile: ProviderProfile,
    page: Any,
    *,
    teach: bool = False,
):
    """Locate the profiled message box through the learned-control surface."""

    return locate_control(
        page,
        provider_id,
        CONTROL_MESSAGE_BOX,
        profile.selectors("message_box"),
        teach=teach,
    )


def response_count(provider_id: str, profile: ProviderProfile, page: Any) -> int:
    return _controls_response_count(page, provider_id, profile.selectors("response"))


def last_response_text(provider_id: str, profile: ProviderProfile, page: Any):
    return locate_response(page, provider_id, profile.selectors("response"))


def rate_limit_visible(page: Any, text: str) -> bool:
    try:
        return text in str(page.locator("body").inner_text(timeout=1000))
    except (cancellation.TaskCancelled, cancellation.DeadlineExceeded):
        raise
    except Exception:
        return False


def poll_late_response(
    read_ready: Callable[[], str],
    *,
    grace: float,
    tick: float,
    default: Callable[[], str] = lambda: "",
) -> str:
    """Poll one driver-provided readiness predicate until ``grace`` expires.

    Cancellation always propagates; every other read failure keeps the
    window open -- the whole point of the late window is that the page is
    mid-update.
    """

    deadline = time.time() + max(0.0, grace)
    while time.time() < deadline:
        try:
            result = read_ready()
            if result:
                return result
        except (cancellation.TaskCancelled, cancellation.DeadlineExceeded):
            raise
        except Exception:
            pass
        cancellation.wait(tick)
    return default()


def clean_whitespace(value: Any, limit: int) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()[:limit]


__all__ = [
    "clean_whitespace",
    "last_response_text",
    "message_box",
    "poll_late_response",
    "rate_limit_visible",
    "response_count",
]