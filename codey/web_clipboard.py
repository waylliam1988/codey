"""Shared clipboard transaction for web chat response copy actions."""

from __future__ import annotations

import time
import uuid

from playwright.sync_api import Locator, Page

from codey import cancellation


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
        previous = page.evaluate("navigator.clipboard.readText()")
    except Exception:
        previous = None

    sentinel = f"__CLIPBOARD_CHECK_{uuid.uuid4().hex}__"
    try:
        page.evaluate("(text) => navigator.clipboard.writeText(text)", sentinel)
    except Exception:
        return ""

    copied = ""
    try:
        cancellation.check()
        action.click()
        deadline = time.time() + max(0.0, timeout)
        while time.time() < deadline:
            copied = page.evaluate("navigator.clipboard.readText()") or ""
            if copied != sentinel:
                break
            cancellation.wait(0.1)
    finally:
        restore = previous if previous is not None else ""
        try:
            page.evaluate("(text) => navigator.clipboard.writeText(text)", restore)
        except Exception:
            pass

    if not copied or copied == sentinel:
        return ""
    return copied.replace("\r\n", "\n").replace("\r", "\n").strip()
