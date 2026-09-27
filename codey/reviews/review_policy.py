"""Review policy: who may review, stated up front.

``web_if_available`` (default) keeps the current smooth behavior: a web
reviewer when one is open, otherwise the writer reviews itself.
``require_web`` fails the review loudly instead of self-reviewing, for users
who want a web model gate. Empty/unset uses the default; any other value is
an explicit configuration error (fail closed, never silently widen).
"""

from __future__ import annotations

import os

from codey.env_names import REVIEW_POLICY_ENV

WEB_IF_AVAILABLE = "web_if_available"
REQUIRE_WEB = "require_web"

REVIEW_POLICIES = (WEB_IF_AVAILABLE, REQUIRE_WEB)


def load_review_policy() -> str:
    """Read the policy from the environment (defaults to web_if_available).

    Raises:
        ValueError: when the value is non-empty and not a known policy.
    """
    raw = os.environ.get(REVIEW_POLICY_ENV, "").strip().lower()
    if not raw:
        return WEB_IF_AVAILABLE
    if raw in REVIEW_POLICIES:
        return raw
    raise ValueError(
        f"invalid {REVIEW_POLICY_ENV} {raw!r}: expected one of {list(REVIEW_POLICIES)}"
    )


def allow_self_review(policy: str) -> bool:
    """Whether the writer may review itself when no web reviewer is open.

    Raises:
        ValueError: when the policy is non-empty and not a known policy.
    """
    normalized = str(policy or "").strip().lower()
    if not normalized:
        return True
    if normalized == REQUIRE_WEB:
        return False
    if normalized == WEB_IF_AVAILABLE:
        return True
    raise ValueError(
        f"invalid review policy {policy!r}: expected one of {list(REVIEW_POLICIES)}"
    )


__all__ = [
    "REQUIRE_WEB",
    "REVIEW_POLICIES",
    "WEB_IF_AVAILABLE",
    "allow_self_review",
    "load_review_policy",
]
