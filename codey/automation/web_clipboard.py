"""Shared clipboard transaction for web chat response copy actions."""

from __future__ import annotations

import contextlib
import uuid
from time import monotonic

from playwright.sync_api import Locator, Page

from codey.runtime.core import cancellation


def copy_action_text(
    page: Page,
    action: Locator,
    *,
    origin: str,
    timeout: float = 2.0,
) -> str:
    """Click a provider copy action and restore the user's clipboard."""
    page.context.grant_permissions(
        ["clipboard-read", "clipboard-write"],
        origin=origin,
    )
    try:
        previous: object = page.evaluate("navigator.clipboard.readText()")
    except Exception:
        return ""
    if not isinstance(previous, str):
        return ""

    sentinel = f"__CLIPBOARD_CHECK_{uuid.uuid4().hex}__"
    copied: str = ""
    try:
        try:
            page.evaluate("(text) => navigator.clipboard.writeText(text)", sentinel)
        except Exception:
            return ""
        cancellation.check()
        action.click()
        deadline = monotonic() + max(0.0, timeout)
        while monotonic() < deadline:
            value: object = page.evaluate("navigator.clipboard.readText()")
            copied = value if isinstance(value, str) else ""
            if copied != sentinel:
                break
            cancellation.wait(0.1)
    finally:
        with contextlib.suppress(Exception):
            page.evaluate("(text) => navigator.clipboard.writeText(text)", previous)

    if not copied or copied == sentinel:
        return ""
    return copied.replace("\r\n", "\n").replace("\r", "\n").strip()
