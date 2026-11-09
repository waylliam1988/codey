"""Experimental page identity; no production Session API or persistent binding store."""
from __future__ import annotations

from dataclasses import dataclass

from playwright.sync_api import Page

from codey.automation.browser import Session


class BrowserTargetBindingError(RuntimeError):
    """The requested page is outside the session's bound browser target."""


@dataclass(frozen=True)
class BrowserTargetBinding:
    """In-process identity for one browser, context and page target."""

    browser_token: int
    context_token: int
    page_token: int


def bind_page(session: Session, page: Page | None = None) -> BrowserTargetBinding:
    """Capture an exact page target owned or borrowed by this session."""
    target = session.page if page is None else page
    contexts = tuple(session.browser.contexts)
    context = target.context
    if context not in contexts or target not in tuple(context.pages):
        raise BrowserTargetBindingError("page is not attached to this browser session")
    return BrowserTargetBinding(id(session.browser), id(context), id(target))

def assert_bound(session: Session, binding: BrowserTargetBinding, page: Page | None = None) -> Page:
    """Reject a closed, replaced, or cross-context page before an action."""
    target = session.page if page is None else page
    actual = bind_page(session, target)
    if actual != binding:
        raise BrowserTargetBindingError("browser target binding does not match the requested page")
    try:
        if target.is_closed():
            raise BrowserTargetBindingError("bound browser page is closed")
    except BrowserTargetBindingError:
        raise
    except Exception as exc:
        raise BrowserTargetBindingError(f"bound browser page cannot be observed: {exc}") from exc
    return target
