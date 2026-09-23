"""Review policy: who may review, stated up front.

``web_if_available`` (default) keeps the current smooth behavior: a web
reviewer when one is open, otherwise the writer reviews itself.
``require_web`` fails the review loudly instead of self-reviewing, for users
who want a web model gate. ``self_review_allowed`` is the explicit opt-in to
the legacy always-allow behavior.
"""

from __future__ import annotations

import os

from codey.env_names import REVIEW_POLICY_ENV

WEB_IF_AVAILABLE = "web_if_available"
REQUIRE_WEB = "require_web"
SELF_REVIEW_ALLOWED = "self_review_allowed"

REVIEW_POLICIES = (WEB_IF_AVAILABLE, REQUIRE_WEB, SELF_REVIEW_ALLOWED)


def load_review_policy() -> str:
    """Read the policy from the environment (defaults to web_if_available)."""
    raw = os.environ.get(REVIEW_POLICY_ENV, "").strip().lower()
    if raw in {REQUIRE_WEB, "requireweb", "web_only", "webonly"}:
        return REQUIRE_WEB
    if raw in {SELF_REVIEW_ALLOWED, "self", "self_review", "allow_self"}:
        return SELF_REVIEW_ALLOWED
    return WEB_IF_AVAILABLE


def allow_self_review(policy: str, *, writer_id: str = "") -> bool:
    """Whether the writer may review itself when no web reviewer is open."""
    normalized = str(policy or "").strip().lower()
    if normalized == REQUIRE_WEB:
        return False
    if normalized in REVIEW_POLICIES:
        return True
    _ = writer_id
    return True


__all__ = [
    "REQUIRE_WEB",
    "REVIEW_POLICIES",
    "SELF_REVIEW_ALLOWED",
    "WEB_IF_AVAILABLE",
    "allow_self_review",
    "load_review_policy",
]
