"""Task-browser request guarding, response facts and bounded diagnostics."""
from __future__ import annotations

import asyncio
import base64
import contextlib
from collections.abc import Awaitable, Callable
from urllib.parse import urlsplit

from playwright.async_api import BrowserContext, Error, Page, Request, Response, Route

from codey.policies.network import check_browser_url
from codey.types.json_value import JsonObject, JsonValue, decode_json_object, require_json_integer, require_json_string

NetworkPolicy = Callable[[JsonObject], Awaitable[JsonObject]]


class BrowserNetwork:
    def __init__(self, policy: NetworkPolicy | None) -> None:
        self.network_policy = policy
        self._network_slots = asyncio.Semaphore(8)
        self._diagnostics: dict[str, list[JsonObject]] = {}
        self._responses: dict[str, JsonObject] = {}
        self._guarded_contexts: set[BrowserContext] = set()
        self._targets: dict[Page, str] = {}
        self._detach: list[Callable[[], None]] = []
        self._route_tasks: set[asyncio.Task[None]] = set()
        self._closing = False

    async def _route(self, route: Route) -> None:
        task = asyncio.create_task(self._run_route(route))
        self._route_tasks.add(task)
        try:
            await task
        except asyncio.CancelledError:
            if not self._closing:
                raise
        finally:
            self._route_tasks.discard(task)

    async def _run_route(self, route: Route) -> None:
        try:
            if self._closing:
                await route.abort()
                return
            # A page's resource burst is unrelated to the tool-command limit.
            # Apply backpressure instead of dropping stylesheets and scripts.
            async with asyncio.timeout(10):
                async with self._network_slots:
                    await self._route_request(route)
        except TimeoutError:
            self._request_event(route.request, "blocked", "route_decision_timeout")
            with contextlib.suppress(Error):
                await route.abort()
        except asyncio.CancelledError:
            with contextlib.suppress(Error):
                await route.abort()
            raise
        except Error as exc:
            if not self._closing:
                self._request_event(route.request, "request_failed", exc.message)

    async def _route_request(self, route: Route) -> None:
        request = route.request
        if self.network_policy is None:
            reason = await asyncio.to_thread(check_browser_url, request.url, request.resource_type, request.is_navigation_request())
            decision: JsonObject = {"decision": "abort" if reason else "continue", "reason": reason or ""}
        else:
            decision = await self.network_policy({"url": request.url, "resource_type": request.resource_type,
                                                  "navigation": request.is_navigation_request()})
        kind = decision.get("decision")
        if kind == "continue":
            await route.continue_()
        elif kind == "abort":
            self._request_event(request, "blocked", str(decision.get("reason") or "policy_denied"))
            await route.abort()
        elif kind == "fulfill":
            headers = decode_json_object(decision.get("headers", {}))
            if not all(isinstance(value, str) for value in headers.values()):
                raise ValueError("invalid route response headers")
            await route.fulfill(status=require_json_integer(decision.get("status", 200), "status"),
                headers={key: require_json_string(value, key) for key, value in headers.items()},
                body=base64.b64decode(require_json_string(decision["body_base64"], "body_base64"), validate=True)
                    if "body_base64" in decision else require_json_string(decision.get("body", ""), "body"))
        else:
            raise ValueError("invalid network policy decision")

    def _request_event(self, request: Request, kind: str, reason: str) -> None:
        try:
            page = request.frame.page
        except Error:
            return
        for watched, target in self._targets.items():
            if watched is page:
                parsed = urlsplit(request.url)
                self._event(target, {"kind": kind, "host": parsed.hostname or "", "path": parsed.path[:200],
                    "resource_type": request.resource_type, "navigation": request.is_navigation_request(),
                    "reason": reason[:300]})

    def _event(self, target: str, event: JsonObject) -> None:
        entries = self._diagnostics.setdefault(target, [])
        entries.append(event)
        del entries[:-64]

    async def guard(self, context: BrowserContext) -> None:
        if context not in self._guarded_contexts:
            await context.route("**/*", self._route)
            self._guarded_contexts.add(context)

    def watch(self, page: Page, target: str) -> None:
        if self._closing:
            return
        self._targets[page] = target
        def request_started(request: Request) -> None:
            if request.is_navigation_request() and request.frame is page.main_frame:
                self._responses.pop(target, None)

        def response_received(response: Response) -> None:
            try:
                request = response.request
                if request.is_navigation_request() and request.frame is page.main_frame:
                    self._responses[target] = {"url": response.url, "http_status": response.status,
                        "content_type": response.headers.get("content-type", "")}
                elif response.status >= 400 and request.resource_type in {"script", "xhr", "fetch"}:
                    self._request_event(request, "request_failed", "HTTP " + str(response.status))
            except Error:
                return

        def request_failed(request: Request) -> None:
            self._request_event(request, "request_failed", request.failure or "request failed")

        def page_error(error: Error) -> None:
            self._event(target, {"kind": "page_error", "reason": error.message[:300]})

        page.on("request", request_started)
        page.on("response", response_received)
        page.on("requestfailed", request_failed)
        page.on("pageerror", page_error)
        def detach() -> None:
            page.remove_listener("request", request_started)
            page.remove_listener("response", response_received)
            page.remove_listener("requestfailed", request_failed)
            page.remove_listener("pageerror", page_error)
        self._detach.append(detach)

    def clear(self, target: str) -> None:
        self._diagnostics[target] = []

    def diagnostics(self, target: str, *, clear: bool) -> list[JsonValue]:
        entries = self._diagnostics.get(target, [])
        if clear:
            self.clear(target)
        return [dict(event) for event in entries]

    def response_facts(self, target: str, url: str) -> JsonObject:
        response = self._responses.get(target, {})
        return {key: value for key, value in response.items() if key != "url"} if response.get("url") == url else {}

    async def close(self) -> None:
        self._closing = True
        try:
            for detach in self._detach:
                with contextlib.suppress(Error):
                    detach()
            for context in tuple(self._guarded_contexts):
                with contextlib.suppress(Error):
                    await context.unroute("**/*", self._route)
            if self._route_tasks:
                await asyncio.wait(tuple(self._route_tasks), timeout=2)
        finally:
            tasks = tuple(self._route_tasks)
            for task in tasks:
                task.cancel()
            try:
                await asyncio.gather(*tasks, return_exceptions=True)
            finally:
                self._route_tasks.clear()
                self._detach.clear()
                self._guarded_contexts.clear()
                self._targets.clear()
                self._responses.clear()
                self._diagnostics.clear()
