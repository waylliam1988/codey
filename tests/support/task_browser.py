"""Real task browser with the production route-policy transport and fixture responses."""
from __future__ import annotations

import json
from collections.abc import Callable

from codey.automation.browser_contracts import BrowserConfig, RouteDecision
from codey.automation.browser_routes import route_policy
from codey.automation.browser_session import BrowserSession
from tests.support.real_cdp import chromium_executable


def browser_for(handler: Callable[[RouteDecision], None], *, owner_task_id: str = "") -> BrowserSession:
    return BrowserSession(BrowserConfig(browser_path=chromium_executable()), network_policy=route_policy(handler), owner_task_id=owner_task_id)


def install(browser: BrowserSession, html: str) -> None:
    # Use the public expression API, keeping all DOM handles inside the worker.
    output = browser.evaluate("(() => {document.open(); document.write(" + json.dumps(html) + "); document.close(); return true;})()")
    assert output.ok, output.model_text
