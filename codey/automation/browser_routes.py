"""Translate synchronous parent egress decisions into the worker's wire protocol."""
from __future__ import annotations

import base64
from collections.abc import Callable
from dataclasses import dataclass, field

from codey.automation.browser_contracts import RouteDecision
from codey.types.json_value import JsonObject, require_json_string


@dataclass(frozen=True)
class RouteRequest:
    url: str
    resource_type: str = "other"
    navigation: bool = False

    def is_navigation_request(self) -> bool:
        return self.navigation


@dataclass
class RouteReply:
    request: RouteRequest
    decision: JsonObject = field(default_factory=lambda: {"decision": "abort"})

    def abort(self, error_code: str | None = None) -> None:
        self.decision = {"decision": "abort", "reason": error_code or "policy_denied"}

    def continue_(self) -> None:
        self.decision = {"decision": "continue"}

    def fulfill(self, *, status: int = 200, content_type: str | None = None,
                headers: dict[str, str] | None = None, body: str | bytes | None = None) -> None:
        response_headers: JsonObject = {
            key: value for key, value in (headers or {}).items()
            if content_type is None or key.lower() != "content-type"
        }
        if content_type is not None:
            response_headers["content-type"] = content_type
        self.decision = {"decision": "fulfill", "status": status, "headers": response_headers}
        if isinstance(body, bytes):
            self.decision["body_base64"] = base64.b64encode(body).decode("ascii")
        else:
            self.decision["body"] = body or ""


def route_policy(handler: Callable[[RouteDecision], None]) -> Callable[[JsonObject], JsonObject]:
    def policy(request: JsonObject) -> JsonObject:
        reply = RouteReply(RouteRequest(require_json_string(request["url"], "url"),
            require_json_string(request.get("resource_type", "other"), "resource type"), request.get("navigation") is True))
        handler(reply)
        return reply.decision
    return policy
