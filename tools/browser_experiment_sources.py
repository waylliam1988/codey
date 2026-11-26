"""Experiment adapters borrow the production task process and source receiver."""
from __future__ import annotations

from pathlib import Path

from codey.automation.browser_contracts import BrowserConfig, RouteDecision
from codey.automation.browser_routes import route_policy
from codey.automation.browser_session import BrowserSession
from codey.policies.network import check_browser_url
from codey.research.browser_page import validate_browser_capture
from codey.research.source_acquisition import with_attempt
from codey.research.source_gateway import OpenedSource, ResearchSourceGateway
from codey.research.task_browser_source import TaskBrowserSource


class ResumableBrowserSearchProvider(TaskBrowserSource):
    def __init__(self, *, headless: bool = True, browser_path: str | None = None, isolated: bool = True,
                 launch: bool = True, cdp_port: int = 0, require_existing_session: bool = False,
                 profile_dir: Path | None = None, bring_to_front: bool = False, owner_task_id: str = "",
                 engine: str | None = None) -> None:
        super().__init__(BrowserSession(BrowserConfig(browser_path=browser_path or "",
            profile_dir=str(profile_dir) if profile_dir is not None else "", cdp_port=cdp_port,
            headless=headless, isolated=isolated and not require_existing_session, launch=launch and not require_existing_session,
            bring_to_front=bring_to_front),
            network_policy=route_policy(lambda reply: self._guard_request(reply)), owner_task_id=owner_task_id), engine=engine)

    def _guard_request(self, route: RouteDecision) -> None:
        reason = check_browser_url(route.request.url, route.request.resource_type, route.request.is_navigation_request())
        route.abort(reason) if reason else route.continue_()

    def resume_fetch(self) -> dict[str, object]:
        try:
            result = validate_browser_capture(self.browser.capture())
        except (OSError, RuntimeError, ValueError) as exc:
            return {"status": "error", "failure_code": "snapshot_unavailable", "detail": str(exc)}
        return with_attempt(result, "browser", str(result.get("url") or ""))

    def close(self) -> None:
        self.browser.close()


class BrowserCaptureGateway(ResearchSourceGateway):
    """Experimental current-page admission reuses the production source receiver."""

    def accept_capture(self, url: str, page: dict[str, object], *, pages: str = "") -> OpenedSource:
        """Receive an already observed page through the same source admission path."""
        return self.capture(url, page, pages=pages)
