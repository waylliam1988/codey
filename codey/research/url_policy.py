"""URL policy for Research web reads."""

from __future__ import annotations

from codey.action_policy import research_url_denial_reason


def check_fetch_url(url: str, *, resolve: bool = True) -> str | None:
    return research_url_denial_reason(url, resolve=resolve)
