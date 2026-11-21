"""Deterministic routes around the real task browser process, with no driver mocks."""
from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass, field

from codey.automation.browser_contracts import BrowserConfig, RouteDecision
from codey.automation.browser_session import BrowserSession
from codey.types.json_value import JsonObject, require_json_string
from tests.support.real_cdp import chromium_executable


@dataclass(frozen=True)
class Request:
    url: str


@dataclass
class Reply:
    request: Request
    decision: JsonObject = field(default_factory=lambda: {"decision": "abort"})

    def abort(self, error_code: str | None = None) -> None:
        self.decision = {"decision": "abort"}

    def continue_(self) -> None:
        self.decision = {"decision": "continue"}

    def fulfill(self, *, status: int = 200, content_type: str | None = None,
                headers: dict[str, str] | None = None, body: str | bytes | None = None) -> None:
        response_headers = dict(headers or {})
        if content_type is not None:
            response_headers["content-type"] = content_type
        self.decision = {"decision": "fulfill", "status": status, "headers": {key: value for key, value in response_headers.items()},
                         "body": body.decode("utf-8") if isinstance(body, bytes) else body or ""}


def browser_for(handler: Callable[[RouteDecision], None], *, owner_task_id: str = "") -> BrowserSession:
    def policy(request: JsonObject) -> JsonObject:
        reply = Reply(Request(require_json_string(request["url"], "url")))
        handler(reply)
        return reply.decision
    return BrowserSession(BrowserConfig(browser_path=chromium_executable()), network_policy=policy, owner_task_id=owner_task_id)


def install(browser: BrowserSession, html: str) -> None:
    # Use the public expression API, keeping all DOM handles inside the worker.
    output = browser.evaluate("(() => {document.open(); document.write(" + json.dumps(html) + "); document.close(); return true;})()")
    assert output.ok, output.model_text
