"""Independent entry to the shared task browser and search coordinator."""
from __future__ import annotations

from pathlib import Path

from codey.automation.browser import DEFAULT_PORT
from codey.automation.browser_contracts import BrowserConfig, RouteDecision
from codey.automation.browser_routes import route_policy
from codey.automation.browser_session import BrowserSession
from codey.policies.network import check_browser_url
from codey.research.task_browser_source import TaskBrowserSource
from codey.research.web_sources import WebSourceProvider
from codey.storage.local_store import DEFAULT_STATE_HOME

RESEARCH_PROFILE = DEFAULT_STATE_HOME / "research-edge-profile"
RESEARCH_CDP_PORT = DEFAULT_PORT + 40


class BrowserSearchProvider(WebSourceProvider):
    """Own a standalone lease; submission and extraction use TaskBrowserSource."""

    def __init__(self, *, engine: str | None = None, profile_dir: Path | None = None,
                 cdp_port: int = RESEARCH_CDP_PORT, browser_path: str | None = None, launch: bool = True,
                 isolated: bool = True, require_existing_session: bool = False,
                 bring_to_front: bool = False, headless: bool = False) -> None:
        self.browser_config = BrowserConfig(browser_path=browser_path or "",
            profile_dir=str(profile_dir or RESEARCH_PROFILE) if not isolated else "",
            cdp_port=cdp_port, headless=headless, isolated=isolated and not require_existing_session,
            launch=launch and not require_existing_session, bring_to_front=bring_to_front)
        self.browser = BrowserSession(self.browser_config,
            network_policy=route_policy(lambda route: self._guard_request(route)))
        super().__init__(browser_factory=lambda: TaskBrowserSource(self.browser, engine=engine, profiles=self.profiles),
                         engine=engine)

    def _guard_request(self, route: RouteDecision) -> None:
        reason = check_browser_url(route.request.url, route.request.resource_type, route.request.is_navigation_request())
        route.abort(reason) if reason else route.continue_()

    def close(self) -> None:
        super().close()
        self.browser.close()
